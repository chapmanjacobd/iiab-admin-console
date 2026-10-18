"""Unit tests for syncweb restart recovery."""

import json
import types


def test_wip_args_recovers_legacy_redacted_command(cmdsrv):
    # older rows stored "SYNCWEB-FETCH-CONTENT ***", which does not parse
    parse_failed, job_info = cmdsrv.parse_cmd_msg('SYNCWEB-FETCH-CONTENT ***')
    assert parse_failed is True
    opt_args = {
        'syncweb_wip': {
            'ticket': 'real-ticket',
            'zim_ids': ['zim-a'],
            'module_ids': ['mod-a'],
        }
    }

    recovered = cmdsrv.syncweb_wip_args(job_info, opt_args)

    assert recovered['ticket'] == 'real-ticket'
    assert recovered['zim_ids'] == ['zim-a']
    assert recovered['module_ids'] == ['mod-a']


def test_wip_args_falls_back_to_spec_file(cmdsrv, tmp_path):
    spec_path = tmp_path / 'syncweb-fetch-1.json'
    spec_path.write_text(
        json.dumps(
            {
                'ticket': 'spec-ticket',
                'zims': [{'id': 'zim-a'}],
                'modules': [{'id': 'mod-a'}],
            }
        )
    )
    job_info = {
        'cmd': 'SYNCWEB-FETCH-CONTENT',
        'cmd_args': {},
        'job_command': 'scripts/iiab-syncweb-fetch.py --spec-file %s' % spec_path,
    }

    recovered = cmdsrv.syncweb_wip_args(job_info, {})

    assert recovered['ticket'] == 'spec-ticket'
    assert recovered['zim_ids'] == ['zim-a']
    assert recovered['module_ids'] == ['mod-a']


def test_wip_args_without_metadata_does_not_raise(cmdsrv):
    job_info = {'cmd': 'SYNCWEB-FETCH-CONTENT', 'cmd_args': {}, 'job_command': 'scripts/x --spec-file /nonexistent'}
    recovered = cmdsrv.syncweb_wip_args(job_info, {})
    assert recovered['ticket'] == ''
    assert recovered['zim_ids'] == []
    assert recovered['module_ids'] == []


def test_oer2go_moddir_uses_catalog_then_falls_back(cmdsrv):
    original = cmdsrv.oer2go_catalog
    try:
        cmdsrv.oer2go_catalog = {'catalog-id': {'moddir': 'module-dir'}}
        assert cmdsrv.oer2go_moddir('catalog-id') == 'module-dir'
        assert cmdsrv.oer2go_moddir('unknown-id') == 'unknown-id'
    finally:
        cmdsrv.oer2go_catalog = original


def test_cleanup_syncweb_folder_leaves_marked_folder(cmdsrv, tmp_path, monkeypatch):
    staging_dir = tmp_path / 'staging'
    folder = staging_dir / 'fetch-1' / 'folder'
    folder.mkdir(parents=True)
    marker = tmp_path / 'syncweb-fetch-1.folder'
    marker.write_text(str(folder))
    binary = tmp_path / 'syncweb'
    binary.touch()

    original = {
        'syncweb_binary': cmdsrv.syncweb_binary,
        'syncweb_user': cmdsrv.syncweb_user,
        'syncweb_data_dir': cmdsrv.syncweb_data_dir,
        'syncweb_staging_dir': cmdsrv.syncweb_staging_dir,
    }
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return types.SimpleNamespace(returncode=0, stdout='', stderr='')

    try:
        cmdsrv.syncweb_binary = str(binary)
        cmdsrv.syncweb_user = 'syncweb'
        cmdsrv.syncweb_data_dir = str(tmp_path / 'data')
        cmdsrv.syncweb_staging_dir = str(staging_dir)
        monkeypatch.setattr(cmdsrv.pwd, 'getpwnam', lambda name: types.SimpleNamespace(pw_name=name, pw_gid=0))
        monkeypatch.setattr(cmdsrv.subprocess, 'run', fake_run)

        assert cmdsrv.cleanup_syncweb_folder(str(marker)) is True
        assert not marker.exists()
        assert calls[-1][0][-3:] == ['folders', 'leave', str(folder.resolve())]
    finally:
        cmdsrv.syncweb_binary = original['syncweb_binary']
        cmdsrv.syncweb_user = original['syncweb_user']
        cmdsrv.syncweb_data_dir = original['syncweb_data_dir']
        cmdsrv.syncweb_staging_dir = original['syncweb_staging_dir']
