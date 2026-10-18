"""Unit tests for the syncweb fetch helper's path, manifest and batching logic."""

import subprocess
import types

import pytest

OWNER = types.SimpleNamespace(pw_name='syncweb', pw_gid=0)


def entry(path, content_id='a' * 64, size=1):
    return {'logical_path': path, 'content_id': content_id, 'size': size}


def test_safe_name(syncweb_fetch):
    assert syncweb_fetch.safe_name('foo.zim')
    assert not syncweb_fetch.safe_name('foo/bar')
    assert not syncweb_fetch.safe_name('.')
    assert not syncweb_fetch.safe_name('..')
    assert not syncweb_fetch.safe_name('')
    assert not syncweb_fetch.safe_name(None)


def test_safe_relative_path(syncweb_fetch):
    assert syncweb_fetch.safe_relative_path('foo')
    assert syncweb_fetch.safe_relative_path('foo/bar.baz')
    assert not syncweb_fetch.safe_relative_path('/etc/passwd')
    assert not syncweb_fetch.safe_relative_path('../escape')
    assert not syncweb_fetch.safe_relative_path('foo/../../escape')
    assert not syncweb_fetch.safe_relative_path('.')
    assert not syncweb_fetch.safe_relative_path('')
    assert not syncweb_fetch.safe_relative_path(None)


def test_package_entry_path(syncweb_fetch):
    assert syncweb_fetch.package_entry_path('modules/foo', 'a/b.txt') == 'modules/foo/a/b.txt'
    with pytest.raises(ValueError):
        syncweb_fetch.package_entry_path('modules/foo', '../escape')


def test_collection_entries_accepts_valid_manifest(syncweb_fetch):
    collected = syncweb_fetch.collection_entries({'entries': [entry('a'), entry('b', 'b' * 64, 2)]})
    assert set(collected) == {'a', 'b'}
    assert collected['b']['size'] == 2


@pytest.mark.parametrize(
    'manifest',
    [
        {'entries': []},
        {'entries': [entry('a'), entry('a')]},
        {'entries': [{'logical_path': '../escape', 'content_id': 'a' * 64, 'size': 1}]},
        {'entries': [{'logical_path': 'a', 'content_id': 'short', 'size': 1}]},
        {'entries': [{'logical_path': 'a', 'content_id': 'a' * 64, 'size': -1}]},
        {'entries': ['not a dict']},
    ],
)
def test_collection_entries_rejects_invalid_manifest(syncweb_fetch, manifest):
    with pytest.raises(ValueError):
        syncweb_fetch.collection_entries(manifest)


def test_check_listing_entry_compares_hash_and_size(syncweb_fetch):
    manifest_entry = entry('a', 'AB' * 32, 10)
    syncweb_fetch.check_listing_entry({'hash': 'ab' * 32, 'size': 10}, manifest_entry, 'a')
    with pytest.raises(ValueError):
        syncweb_fetch.check_listing_entry({'hash': 'cd' * 32, 'size': 10}, manifest_entry, 'a')
    with pytest.raises(ValueError):
        syncweb_fetch.check_listing_entry({'hash': 'ab' * 32, 'size': 11}, manifest_entry, 'a')


def test_verify_listing_requires_every_entry(syncweb_fetch):
    allowed = entry('a', 'a' * 64)
    syncweb_fetch.verify_listing({'a': {'hash': 'a' * 64, 'size': 1}}, {'a': allowed})
    with pytest.raises(FileNotFoundError):
        syncweb_fetch.verify_listing({}, {'a': allowed})


def test_download_package_entries_downloads_package_prefix(syncweb_fetch, monkeypatch):
    entries = {
        'modules/foo/a': entry('a', 'a' * 64, 1),
        'modules/foo/b': entry('b', 'b' * 64, 2),
        'modules/foo/c': entry('c', 'b' * 64, 2),
    }
    listings = []

    def fake_listing(binary, data_dir, owner, folder, *selectors):
        listings.append(selectors)
        return {
            path: {'hash': item['content_id'], 'size': item['size']}
            for path, item in entries.items()
        }

    downloads = []

    def fake_run(binary, data_dir, owner, *args):
        downloads.append(list(args))

    monkeypatch.setattr(syncweb_fetch, 'collection_listing', fake_listing)
    monkeypatch.setattr(syncweb_fetch, 'run_syncweb', fake_run)

    syncweb_fetch.download_package_entries(
        '/usr/local/bin/syncweb', '/var/lib/syncweb/data', OWNER, None, 'modules/foo', entries
    )

    # one listing before and one after, both scoped to the package
    assert [selectors for selectors in listings] == [
        ('--path-prefix', 'modules/foo'),
        ('--path-prefix', 'modules/foo'),
    ]
    assert downloads == [['download', 'None', '--path-prefix', 'modules/foo']]


def test_download_package_entries_verifies_before_downloading(syncweb_fetch, monkeypatch):
    monkeypatch.setattr(
        syncweb_fetch,
        'collection_listing',
        lambda binary, data_dir, owner, folder, *selectors: {},
    )
    downloads = []
    monkeypatch.setattr(
        syncweb_fetch,
        'run_syncweb',
        lambda binary, data_dir, owner, *args: downloads.append(args),
    )

    with pytest.raises(FileNotFoundError):
        syncweb_fetch.download_package_entries(
            '/usr/local/bin/syncweb',
            '/var/lib/syncweb/data',
            OWNER,
            None,
            'modules/foo',
            {'modules/foo/a': entry('a')},
        )
    assert downloads == []


def test_cleanup_marker_round_trip(syncweb_fetch, tmp_path):
    folder = tmp_path / 'fetch-1' / 'folder'
    folder.mkdir(parents=True)
    marker = tmp_path / 'syncweb-fetch-1.folder'

    syncweb_fetch.write_cleanup_marker(marker, folder)
    assert marker.read_text() == str(folder)

    syncweb_fetch.remove_cleanup_marker(marker)
    assert not marker.exists()


def test_run_syncweb_reports_stderr_on_failure(syncweb_fetch, monkeypatch):
    def fail(command, **kwargs):
        raise subprocess.CalledProcessError(1, command, stderr=b'daemon not running\n')

    monkeypatch.setattr(syncweb_fetch.subprocess, 'run', fail)

    with pytest.raises(RuntimeError, match='daemon not running'):
        syncweb_fetch.run_syncweb('/usr/local/bin/syncweb', '/var/lib/syncweb/data', OWNER, 'ls')


def test_syncweb_json_reports_stderr_for_invalid_json(syncweb_fetch, monkeypatch):
    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, stdout=b'not json', stderr=b'unexpected output')

    monkeypatch.setattr(syncweb_fetch.subprocess, 'run', fake_run)

    with pytest.raises(RuntimeError, match='unexpected output'):
        syncweb_fetch.syncweb_json('/usr/local/bin/syncweb', '/var/lib/syncweb/data', OWNER, 'ls')


def test_installed_helpers(syncweb_fetch, tmp_path):
    spec = {
        'zim_content_dir': str(tmp_path / 'zims'),
        'modules_dir': str(tmp_path / 'modules'),
    }
    (tmp_path / 'zims').mkdir()
    (tmp_path / 'modules' / 'foo').mkdir(parents=True)

    assert not syncweb_fetch.zim_installed(spec, 'foo')
    (tmp_path / 'zims' / 'foo.zim').write_text('zim')
    assert syncweb_fetch.zim_installed(spec, 'foo')

    assert syncweb_fetch.module_installed(spec, 'foo')
    assert not syncweb_fetch.module_installed(spec, 'bar')
