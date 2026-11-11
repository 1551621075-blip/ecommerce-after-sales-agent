#!/usr/bin/env python3
"""Offline, non-editable wheel/sdist installation check; TEST only, not LLM acceptance.

Run with Python 3.12. All uv commands are offline, config-free, and forbid Python
downloads. Install the frozen runtime manifest and wheel with --no-deps, then
require uv pip check and exact installed-version agreement with uv.lock. This is
an offline fully locked install check, NOT a fresh dependency-resolver check.
Missing cached packages are a failure; this script never retries online.
The caller may separately authorize/cache dependencies, then rerun this script.
Only this script's newly created /tmp work tree is removed; a safe JSON report stays.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
import zipfile
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1]
DEFAULT_UV = Path('/Library/Frameworks/Python.framework/Versions/3.12/bin/uv')
DEFAULT_PYTHON = Path('/Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12')
DEFAULT_CACHE = Path.home() / '.cache' / 'uv'
DIST_NAME = 'ecommerce-after-sales-agent'
ASSETS = ('templates/index.html', 'static/app.js', 'static/style.css')

# Runs only with the newly created venv interpreter, from an external cwd. Never
# execute this probe in the source environment or recover from a bad wheel by
# adding the source to sys.path. It intentionally exercises the console entry too.
INSTALLED_PROBE = r'''
import importlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import sys
from html.parser import HTMLParser
from urllib.parse import urlsplit

venv, source, data, credential_path, output, manifest_path, lock_versions_path = map(Path, sys.argv[1:])
venv, source = venv.resolve(), source.resolve()

def require(value, code):
    if not value:
        raise RuntimeError(code)

def inside(file, parent):
    return Path(file).resolve().is_relative_to(parent)

require(Path(sys.prefix).resolve() == venv, 'wrong_venv_prefix')
require(sys.prefix != sys.base_prefix, 'not_a_new_venv')
require('PYTHONPATH' not in os.environ, 'pythonpath_leaked')
require(not Path.cwd().resolve().is_relative_to(source), 'source_cwd')
require(not any(p and inside(p, source) for p in sys.path), 'source_import_path')

modules = {}
for name in ('after_sales', 'after_sales.api', 'after_sales.cli', 'after_sales.storage',
             'after_sales.demo_provider', 'fastapi', 'httpx', 'uvicorn'):
    module = importlib.import_module(name)
    require(module.__file__ and inside(module.__file__, venv), 'module_outside_new_venv')
    modules[name] = str(Path(module.__file__).resolve())
package = importlib.import_module('after_sales')
require(all(inside(p, venv) for p in package.__path__), 'namespace_outside_venv')
dist = importlib.metadata.distribution('ecommerce-after-sales-agent')
require(inside(dist.locate_file(''), venv), 'metadata_outside_new_venv')
entry = [e for e in dist.entry_points if e.group == 'console_scripts' and e.name == 'after-sales']
require(len(entry) == 1 and callable(entry[0].load()), 'broken_console_entry')
direct = dist.read_text('direct_url.json')
require(direct is not None, 'wheel_install_origin_missing')
direct = json.loads(direct)
require(not direct.get('dir_info', {}).get('editable'), 'editable_install')
require(urlsplit(direct['url']).path.endswith('.whl'), 'not_installed_from_wheel')

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

# uv export preserves environment markers; validate only this interpreter's
# applicable runtime closure, and reject missing/extra/unlocked distributions.
lock_versions = json.loads(lock_versions_path.read_text(encoding='utf-8'))
expected = {}
for line in manifest_path.read_text(encoding='utf-8').splitlines():
    if not line.strip() or line.lstrip().startswith('#'):
        continue
    requirement = Requirement(line)
    require(requirement.url is None, 'nonregistry_runtime_requirement')
    if requirement.marker is not None and not requirement.marker.evaluate():
        continue
    name = canonicalize_name(requirement.name)
    specs = list(requirement.specifier)
    require(len(specs) == 1 and specs[0].operator == '==' and '*' not in specs[0].version,
            'runtime_requirement_not_exact')
    version = specs[0].version
    require(name in lock_versions and version in lock_versions[name], 'manifest_version_not_in_lock')
    require(name not in expected or expected[name] == version, 'conflicting_runtime_versions')
    expected[name] = version
require(expected and 'ecommerce-after-sales-agent' not in expected, 'invalid_runtime_manifest')
actual = {}
for installed in importlib.metadata.distributions():
    name = canonicalize_name(installed.metadata['Name'])
    require(name not in actual, 'duplicate_installed_distribution')
    require(inside(installed.locate_file(''), venv), 'dependency_outside_new_venv')
    actual[name] = installed.version
require(set(actual) == set(expected) | {'ecommerce-after-sales-agent'}, 'installed_dependency_set_mismatch')
require(all(actual[name] == version for name, version in expected.items()), 'installed_version_differs_from_lock')

from after_sales.api import create_app
from after_sales.demo_provider import DemoProvider
from after_sales.storage import Store
from fastapi.testclient import TestClient

credentials = json.loads(credential_path.read_text(encoding='utf-8'))
require(isinstance(credentials, dict), 'credential_schema_changed')
token = credentials.get('alice')
require(isinstance(token, str) and re.fullmatch(r'[A-Za-z0-9._~+/-]{1,128}={0,2}', token),
        'credential_schema_changed')
provider = DemoProvider()
require(getattr(provider, 'is_test_provider', False) is True, 'not_explicit_test_provider')
# TestClient is in-process ASGI; this origin opens no listener and probes no port.
origin = 'http://127.0.0.1:18763'
app = create_app(Store(data), provider, origin=origin)

class References(HTMLParser):
    def __init__(self):
        super().__init__()
        self.resources = []
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'script' and 'src' in attrs:
            self.resources.append(attrs['src'])
        if tag == 'link' and attrs.get('rel') == 'stylesheet' and 'href' in attrs:
            self.resources.append(attrs['href'])

asset_results = []
asset_root = Path(package.__file__).resolve().parent
with TestClient(app, base_url=origin, follow_redirects=False) as client:
    identity = client.get('/api/me', headers={'Authorization': 'Bearer ' + token})
    require(identity.status_code == 200, 'test_identity_failed')
    me = identity.json()
    require(me['user_id'] == 'alice' and me['role'] == 'customer' and me['test_mode'] is True,
            'test_identity_mismatch')
    routes = [('/', 'templates/index.html', 'text/html'),
              ('/static/app.js', 'static/app.js', 'javascript'),
              ('/static/style.css', 'static/style.css', 'text/css')]
    for route, relative, content_type in routes:
        file = asset_root / relative
        require(inside(file, venv) and file.is_file(), 'packaged_asset_missing')
        response = client.get(route)
        require(response.status_code == 200, 'static_resource_failed')
        require(content_type in response.headers.get('content-type', ''), 'static_content_type')
        require(response.content and response.content == file.read_bytes(), 'asset_not_from_installed_package')
        require(response.headers.get('x-content-type-options') == 'nosniff', 'missing_security_header')
        if route == '/':
            refs = References()
            refs.feed(response.text)
            require('/static/app.js' in refs.resources and '/static/style.css' in refs.resources,
                    'html_asset_references_missing')
            require(all(r.startswith('/static/') and not r.startswith('//')
                        and not urlsplit(r).netloc and not urlsplit(r).query
                        for r in refs.resources), 'nonlocal_static_reference')
        asset_results.append({'route': route, 'package_path': str(file), 'bytes': len(response.content)})
    for route in set(refs.resources):
        require(client.get(route).status_code == 200, 'html_referenced_resource_missing')
# No raw HTTP bodies, credential values, or conversations leave this subprocess.
result = {'explicit_test_mode': True, 'noneditable_wheel': True,
          'console_entry': entry[0].value, 'package_version': dist.version,
          'modules': modules, 'resources': asset_results, 'outside_source_cwd': True,
          'runtime_versions_match_lock': True, 'runtime_versions': dict(sorted(expected.items()))}
with output.open('x', encoding='utf-8') as stream:
    json.dump(result, stream, ensure_ascii=False)
    stream.write('\n')
'''


class CheckFailure(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def require(value, code: str):
    if not value:
        raise CheckFailure(code)


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--uv', type=Path, default=DEFAULT_UV, help='absolute uv executable')
    parser.add_argument('--python', type=Path, default=DEFAULT_PYTHON, help='absolute Python 3.12 executable')
    parser.add_argument('--cache-dir', type=Path, default=DEFAULT_CACHE, help='existing public uv package cache')
    parser.add_argument('--timeout-seconds', type=int, default=240, help='per-command timeout (30..1800)')
    return parser.parse_args()


def same_directory(path: Path, identity):
    current = path.lstat()
    return stat.S_ISDIR(current.st_mode) and (current.st_dev, current.st_ino) == identity


def group_exists(pid: int) -> bool:
    try:
        os.killpg(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def stop_owned_group(process: subprocess.Popen) -> bool:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if not group_exists(process.pid):
            break
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            break
        except PermissionError:
            return False
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            process.poll()  # Reap our direct child; do not mistake its zombie for a live group.
            if not group_exists(process.pid):
                break
            time.sleep(0.05)
    process.poll()
    return not group_exists(process.pid)


def dependency_diagnostic(stderr: bytes, known_versions: dict) -> dict:
    """Never publish raw stderr, URLs, paths, arbitrary words or unknown packages."""
    text = stderr.decode('utf-8', 'replace').lower()
    reason_phrases = {
        'resolution_unsatisfiable': ('no solution found',),
        'offline_network_disabled': ('network is disabled', 'network access is disabled',
                                     'network connectivity is disabled'),
        'registry_metadata_or_artifact_uncached': ('needs to be downloaded from a registry',),
        'cache_entry_missing': ('not found in the cache', 'not cached', 'no cache entry'),
        'dependency_conflict': ('incompatible', 'requires a different version'),
        'permission_denied': ('permission denied',),
        'unsupported_option': ('unexpected argument',),
    }
    reasons = [reason for reason, phrases in reason_phrases.items() if any(p in text for p in phrases)]
    mentioned = []
    for name, versions in sorted(known_versions.items()):
        if re.search(r'(?<![a-z0-9_.-])' + re.escape(name) + r'(?![a-z0-9_.-])', text):
            mentioned.append({'package': name, 'locked_versions': sorted(versions)})
    return {'reasons': reasons or ['unclassified_offline_command_failure'],
            'mentioned_locked_packages': mentioned}


def run(args) -> int:
    summary = {'kind': 'offline_dist_install_check', 'llm_acceptance': False, 'ok': False,
               'network_policy': 'offline_only_no_automatic_online_retry', 'steps': [],
               'install_strategy': 'offline_fully_locked_install',
               'fresh_dependency_resolution_tested': False,
               'validation_policy': 'frozen_runtime_manifest_no_deps_then_pip_check_and_exact_lock_versions'}
    root = work = report = None
    root_identity = work_identity = report_identity = None
    children = []
    phase = 'prepare'
    previous_handlers = {}

    def interrupted(signum, _frame):
        raise CheckFailure('interrupted_' + signal.Signals(signum).name)

    try:
        require(sys.version_info[:2] == (3, 12), 'run_script_with_python_3_12')
        require(sys.platform == 'darwin', 'this_check_targets_macos')
        require(30 <= args.timeout_seconds <= 1800, 'invalid_timeout')
        for executable in (args.uv, args.python):
            require(executable.is_absolute() and executable.is_file()
                    and os.access(executable, os.X_OK), 'absolute_executable_missing')
        require(args.cache_dir.is_absolute() and args.cache_dir.is_dir(), 'existing_offline_cache_required')
        lock_bytes = (SOURCE / 'uv.lock').read_bytes()
        lock = tomllib.loads(lock_bytes.decode('utf-8'))
        known_versions = {}
        for package in lock['package']:
            name, version = package['name'], package['version']
            require(re.fullmatch(r'[a-z0-9][a-z0-9-]*', name)
                    and re.fullmatch(r'[0-9][A-Za-z0-9.!+_-]*', version), 'unsafe_lock_name_or_version')
            if name != DIST_NAME:
                require(package.get('source') == {'registry': 'https://pypi.org/simple'},
                        'only_public_pypi_lock_sources_supported')
            known_versions.setdefault(name, set()).add(version)
        summary['lock_sha256'] = hashlib.sha256(lock_bytes).hexdigest()
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[sig] = signal.signal(sig, interrupted)
        root = Path(tempfile.mkdtemp(prefix='after-sales-install-', dir='/tmp')).resolve()
        require(not root.is_relative_to(SOURCE), 'temporary_directory_inside_source')
        root.chmod(0o700)
        root_stat = root.lstat()
        root_identity = (root_stat.st_dev, root_stat.st_ino)
        work, report = root / 'work', root / 'report'
        work.mkdir(mode=0o700)
        work_stat = work.lstat()
        work_identity = (work_stat.st_dev, work_stat.st_ino)
        report.mkdir(mode=0o700)
        report_stat = report.lstat()
        report_identity = (report_stat.st_dev, report_stat.st_ino)
        home, temp, dist_dir, cwd = (work / name for name in ('home', 'tmp', 'dist', 'outside-source-cwd'))
        for directory in (home, temp, dist_dir, cwd):
            directory.mkdir(mode=0o700)
        env = {'PATH': '/usr/bin:/bin', 'HOME': str(home), 'TMPDIR': str(temp), 'LANG': 'en_US.UTF-8',
               'XDG_CONFIG_HOME': str(home), 'XDG_CACHE_HOME': str(temp),
               'PYTHONNOUSERSITE': '1', 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONUNBUFFERED': '1',
               'UV_OFFLINE': '1', 'UV_NO_CONFIG': '1', 'UV_PYTHON_DOWNLOADS': 'never',
               'UV_CACHE_DIR': str(args.cache_dir), 'UV_PYTHON_INSTALL_DIR': str(work / 'uv-python'),
               'PIP_NO_INDEX': '1', 'PIP_DISABLE_PIP_VERSION_CHECK': '1', 'PIP_CONFIG_FILE': os.devnull}
        # Whitelisting excludes PYTHONPATH, VIRTUAL_ENV, proxies, API tokens,
        # UV_INDEX credentials, parent-project configuration and user site-packages.
        uv = [str(args.uv), '--offline', '--no-config', '--no-python-downloads',
              '--cache-dir', str(args.cache_dir)]

        def command(name, argv, *, capture=False, dependency_phase=False):
            nonlocal phase
            phase = name
            process = subprocess.Popen(argv, cwd=cwd, env=env,
                                       stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                                       stderr=subprocess.PIPE if dependency_phase else subprocess.DEVNULL,
                                       start_new_session=True)
            children.append(process)
            try:
                output, stderr = process.communicate(timeout=args.timeout_seconds)
            except subprocess.TimeoutExpired:
                stop_owned_group(process)
                raise CheckFailure(name + '_timeout') from None
            if process.returncode != 0 and dependency_phase:
                summary['dependency_diagnostic'] = dependency_diagnostic(stderr or b'', known_versions)
            require(process.returncode == 0, name + '_exit_' + str(process.returncode))
            require(not group_exists(process.pid), name + '_left_child_processes')
            summary['steps'].append(name)
            return output

        command('python_3_12_available', [str(args.python), '-I', '-B', '-c',
                'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)'])
        command('build_sdist_offline', uv + ['build', '--python', str(args.python), '--sdist',
                '--out-dir', str(dist_dir), str(SOURCE)], dependency_phase=True)
        sdists = list(dist_dir.glob('*.tar.gz'))
        require(len(sdists) == 1, 'expected_one_sdist')
        with tarfile.open(sdists[0], 'r:gz') as archive:
            names = archive.getnames()
            require(all(any(name.endswith('/src/after_sales/' + asset) for name in names)
                        for asset in ASSETS), 'sdist_missing_html_js_css')
        summary['steps'].append('sdist_contains_html_js_css')
        # Build the wheel from the sdist, not directly from source. This detects
        # missing sdist resources as well as broken wheel packaging.
        command('build_wheel_from_sdist_offline', uv + ['build', '--python', str(args.python),
                '--wheel', '--out-dir', str(dist_dir), str(sdists[0])], dependency_phase=True)
        wheels = list(dist_dir.glob('*.whl'))
        require(len(wheels) == 1, 'expected_one_wheel')
        with zipfile.ZipFile(wheels[0]) as archive:
            names = set(archive.namelist())
            require(all('after_sales/' + asset in names for asset in ASSETS), 'wheel_missing_html_js_css')
            require(not any(name.endswith('.pth') for name in names), 'wheel_contains_path_injection')
        summary['artifacts'] = {'sdist': sdists[0].name, 'wheel': wheels[0].name}
        summary['steps'].append('wheel_contains_html_js_css')

        manifest = work / 'frozen-runtime-requirements.txt'
        lock_versions_file = work / 'locked-versions.json'
        lock_versions_file.write_text(json.dumps({n: sorted(v) for n, v in known_versions.items()}), encoding='utf-8')
        command('export_frozen_runtime_manifest', uv + ['export', '--frozen', '--project', str(SOURCE),
                '--no-dev', '--no-emit-project', '--no-hashes', '--no-header', '--output-file', str(manifest)],
                dependency_phase=True)
        require((SOURCE / 'uv.lock').read_bytes() == lock_bytes, 'lock_changed_during_check')
        for line in manifest.read_text(encoding='utf-8').splitlines():
            if not line.strip() or line.lstrip().startswith('#'):
                continue
            requirement = re.fullmatch(r'([a-z0-9][a-z0-9-]*)==([0-9][A-Za-z0-9.!+_-]*)(?:\s*;\s*[^\n]+)?', line)
            require(requirement is not None, 'unsupported_frozen_requirement')
            name, version = requirement.group(1, 2)
            require(name != DIST_NAME and version in known_versions.get(name, set()), 'manifest_not_locked_runtime')
        venv = work / 'fresh-venv'
        command('create_fresh_venv', uv + ['venv', '--python', str(args.python), str(venv)])
        python = venv / 'bin/python'
        # This is deliberately not claimed as fresh resolver acceptance. uv can
        # install cached artifacts while registry dependency metadata is absent.
        # The lock's full runtime manifest supplies exact requirements; pip check
        # plus the installed probe must validate the resulting dependency closure.
        command('install_wheel_and_frozen_runtime_offline', uv + ['pip', 'install', '--python', str(python),
                '--no-deps', '--requirement', str(manifest), str(wheels[0])], dependency_phase=True)
        command('installed_dependency_consistency', uv + ['pip', 'check', '--python', str(python)],
                dependency_phase=True)
        # CLI is deliberately NOT replaced with python -m; bad entry points fail.
        cli = venv / 'bin/after-sales'
        require(cli.is_file(), 'installed_console_script_missing')
        help_output = command('installed_cli_help_outside_source', [str(cli), '--help'], capture=True)
        require(b'init' in help_output and b'serve' in help_output, 'console_help_missing_commands')
        data, credentials = work / 'synthetic-data', work / 'install-check-owned-credentials.json'
        command('installed_cli_init_outside_source', [str(cli), 'init', '--data-dir', str(data),
                '--credentials', str(credentials)])
        credential_stat = credentials.lstat()
        require(stat.S_ISREG(credential_stat.st_mode) and credential_stat.st_uid == os.getuid()
                and credential_stat.st_mode & 0o077 == 0 and credential_stat.st_size < 16384,
                'owned_credentials_not_private')
        result_file = work / 'installed-probe.json'
        command('installed_testclient_static_resources_and_package_paths',
                [str(python), '-I', '-B', '-c', INSTALLED_PROBE, str(venv), str(SOURCE), str(data),
                 str(credentials), str(result_file), str(manifest), str(lock_versions_file)])
        # Probe output has a closed, safe schema; no captured process logs are kept.
        probe = json.loads(result_file.read_text(encoding='utf-8'))
        require(set(probe) == {'explicit_test_mode', 'noneditable_wheel', 'console_entry',
                              'package_version', 'modules', 'resources', 'outside_source_cwd',
                              'runtime_versions_match_lock', 'runtime_versions'}, 'probe_schema_changed')
        require(probe['runtime_versions_match_lock'] is True, 'runtime_versions_not_verified')
        require((SOURCE / 'uv.lock').read_bytes() == lock_bytes, 'lock_changed_during_check')
        summary['steps'].append('all_runtime_dependency_versions_match_frozen_lock')
        summary['installed'] = probe
        credentials.unlink()
        summary['ok'] = True
    except CheckFailure as error:
        summary['failure'] = {'phase': phase, 'code': error.code}
    except Exception:
        # Do not serialize exception text, stderr, HTTP bodies, token files, or chats.
        summary['failure'] = {'phase': phase, 'code': 'unexpected_local_error'}
    finally:
        # Keep repeated interrupts from skipping cleanup. SIGKILL cannot be handled.
        for sig in previous_handlers:
            signal.signal(sig, signal.SIG_IGN)
        stopped = True
        for process in reversed(children):
            if not stop_owned_group(process):
                stopped = False
        summary['children_stopped'] = stopped
        summary['owned_work_removed'] = False
        if not stopped:
            summary['ok'] = False
            summary['cleanup_failure'] = 'owned_child_group_not_stopped'
        if work is not None and stopped:
            try:
                require(same_directory(root, root_identity) and same_directory(work, work_identity),
                        'cleanup_owner_identity_changed')
                require(work.resolve() == root / 'work', 'cleanup_path_changed')
                shutil.rmtree(work)  # Only the inode/path we created; never a user-provided tree.
                summary['owned_work_removed'] = True
            except Exception:
                summary['ok'] = False
                summary['cleanup_failure'] = 'owned_work_cleanup_failed'
        if not summary['ok']:
            summary['hint'] = ('No online retry was attempted. Check the failure phase, Python/uv paths, '
                               'offline cache contents, packaging and console entry. Any network/cache '
                               'preparation requires separate caller authorization.')
        if report is not None:
            summary['report_dir'] = str(report)
            try:
                require(same_directory(root, root_identity) and same_directory(report, report_identity),
                        'report_owner_identity_changed')
                output = report / 'result.json'
                descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                    json.dump(summary, stream, ensure_ascii=False, indent=2)
                    stream.write('\n')
            except Exception:
                summary['ok'] = False
                summary['report_failure'] = 'report_write_failed'
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(run(parse_args()))
