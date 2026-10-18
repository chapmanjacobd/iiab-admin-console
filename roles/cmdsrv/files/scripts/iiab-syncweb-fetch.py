#!/usr/bin/python3

import argparse
import glob
import json
import os
import pwd
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

# unit installed by roles/syncweb/templates/syncweb.service.j2
SYNCWEB_SERVICE = "syncweb.service"

# Package paths inside a syncweb folder (see docs/6.Syncweb.md): each source
# package has its own root under packages/ and keeps these logical paths in its
# collection manifest.
ZIM_PREFIX = "zims/"
MODULE_PREFIX = "modules/"
COLLECTION_ROOT = "collections"


def run_syncweb(binary, data_dir, owner, *args, stdin=None, capture=False):
    command = [
        binary,
        "--data-dir",
        data_dir,
        "--no-color",
        "--json",
    ]
    command.extend(args)
    # Run as the syncweb user, never as root: everything the client writes
    # (identity key, node database, lock file, folder metadata) must stay owned
    # by the same user as syncweb.service.  user=/group=/extra_groups= need
    # Python >= 3.9 (Debian 11 and later); extra_groups=[] drops the root
    # supplementary groups.
    env = {key: value for key, value in os.environ.items() if key != "XDG_RUNTIME_DIR"}
    env["HOME"] = str(Path(data_dir).parent)
    try:
        return subprocess.run(
            command,
            check=True,
            user=owner.pw_name,
            group=owner.pw_gid,
            extra_groups=[],
            env=env,
            input=stdin,
            capture_output=capture,
        )
    except subprocess.CalledProcessError as exc:
        # With capture_output the child's diagnostics never reach the job log;
        # repeat them on the error the job reports.
        stderr = (exc.stderr or b"").decode("utf-8", "replace").strip()
        if stderr:
            raise RuntimeError(
                "syncweb " + (args[0] if args else "command") + " failed: " + stderr
            ) from exc
        raise


def syncweb_json(binary, data_dir, owner, *args, stdin=None):
    completed = run_syncweb(binary, data_dir, owner, *args, stdin=stdin, capture=True)
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        stderr = (completed.stderr or b"").decode("utf-8", "replace").strip()
        raise RuntimeError(
            "syncweb "
            + (args[0] if args else "command")
            + " returned invalid JSON"
            + (": " + stderr if stderr else "")
        ) from exc


def safe_name(value):
    # a bare "." or ".." is a valid basename but not a valid file/dir name
    if not isinstance(value, str) or value in (".", ".."):
        return False
    return bool(value) and os.path.basename(value) == value


def safe_relative_path(value):
    if not isinstance(value, str) or not value or value == ".":
        return False
    path = Path(value)
    return not path.is_absolute() and ".." not in path.parts


def safe_package_name(value):
    return isinstance(value, str) and safe_relative_path(value)


def require_daemon_service(service):
    # The CLI starts a daemon of its own when none is running.  That daemon is
    # unsupervised: it keeps the store lock and the media port, so a later
    # `systemctl start` of the service would restart-loop until it is stopped.
    # Require the supervised daemon instead of spawning one.
    if not os.path.exists("/run/systemd/system"):
        return
    if subprocess.run(["systemctl", "is-active", "--quiet", service]).returncode != 0:
        raise RuntimeError(
            service
            + " is not running; start it before fetching content"
            + " (the syncweb client would otherwise start an unsupervised daemon)"
        )


def set_owner(path, owner):
    if os.geteuid() != owner.pw_uid:
        os.chown(path, owner.pw_uid, owner.pw_gid)
    os.chmod(path, 0o770)


def collection_entries(manifest):
    # syncweb's collection manifest is the contract: it lists every file of the
    # published content set with the BLAKE3 content id and size syncweb uses to
    # verify the content-addressed transfer.  Reject anything that is not a safe
    # relative path so a hostile source cannot direct the install elsewhere.
    entries = manifest.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ValueError("collection manifest lists no content")
    by_path = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("invalid collection manifest entry")
        path = entry.get("logical_path")
        content_id = entry.get("content_id")
        size = entry.get("size")
        if (
            not safe_relative_path(path)
            or not isinstance(content_id, str)
            or len(content_id) != 64
            or not isinstance(size, int)
            or size < 0
        ):
            raise ValueError("invalid collection manifest entry " + str(path))
        if path in by_path:
            raise ValueError("duplicate collection manifest path " + path)
        by_path[path] = entry
    return by_path


def collection_listing(binary, data_dir, owner, folder, *selectors):
    """Return {path: listing row} for the folder, filtered by selectors.

    Each selector is a pair such as ("--path-prefix", value) or
    ("--path-glob", value).  Passing the selector explicitly lets callers
    select an exact path without treating it as a glob pattern.
    """
    listing = syncweb_json(binary, data_dir, owner, "ls", str(folder), *selectors)
    rows = {}
    for row in listing.get("entries", []):
        if isinstance(row, dict) and safe_relative_path(row.get("path")):
            rows[row["path"]] = row
    return rows


def listing_hash(row, label):
    value = row.get("hash")
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError("syncweb listing has no valid content hash for " + label)
    return value.lower()


def check_listing_entry(row, entry, label):
    actual_hash = listing_hash(row, label)
    expected_hash = entry["content_id"].lower()
    if actual_hash != expected_hash:
        raise ValueError(
            "syncweb content hash does not match the collection manifest for "
            + label
            + ": expected "
            + expected_hash
            + ", got "
            + actual_hash
        )
    if row.get("size") != entry["size"]:
        raise ValueError("syncweb content size does not match the collection manifest for " + label)


def verify_listing(listing, entries):
    """Bind every manifest entry to the live folder listing.

    The listing row for a path carries the content id and size syncweb will
    transfer; comparing them with the manifest rejects a same-sized
    replacement before anything is installed.
    """
    for path, entry in entries.items():
        row = listing.get(path)
        if row is None:
            raise FileNotFoundError("syncweb listing does not contain " + path)
        check_listing_entry(row, entry, path)


def download_package_entries(binary, data_dir, owner, folder, package_name, entries):
    """Fetch the blobs listed in one package manifest.

    The manifest is the authoritative file set.  A single prefix listing is
    verified against it, then the package prefix is downloaded in one request.
    Hash-filtered downloads require a separate blob provider ticket in the
    pinned syncweb CLI, while a folder path filter can fetch the package from
    the joined folder directly.  Installation still uses only the manifest
    entries, so any extra files in the source are ignored.
    """
    verify_listing(
        collection_listing(binary, data_dir, owner, folder, "--path-prefix", package_name),
        entries,
    )

    run_syncweb(
        binary,
        data_dir,
        owner,
        "download",
        str(folder),
        "--path-prefix",
        package_name,
    )

    verify_listing(
        collection_listing(binary, data_dir, owner, folder, "--path-prefix", package_name),
        entries,
    )


def download_document_entry(binary, data_dir, owner, folder, path, expected_hash=None):
    # --path-prefix matches the entry itself (and any children); a document
    # entry is a leaf, and a prefix is not a glob, so path metacharacters are
    # matched literally.
    listing = collection_listing(binary, data_dir, owner, folder, "--path-prefix", path)
    row = listing.get(path)
    if row is None:
        raise FileNotFoundError("syncweb listing does not contain " + path)
    actual_hash = listing_hash(row, path)
    if expected_hash is not None and actual_hash != expected_hash.lower():
        raise ValueError(
            "syncweb document hash changed for "
            + path
            + ": expected "
            + expected_hash
            + ", got "
            + actual_hash
        )
    expected_size = row.get("size")
    run_syncweb(binary, data_dir, owner, "download", str(folder), "--path-prefix", path)
    after = collection_listing(binary, data_dir, owner, folder, "--path-prefix", path).get(path)
    if after is None:
        raise FileNotFoundError("syncweb listing does not contain " + path)
    if listing_hash(after, path) != actual_hash or after.get("size") != expected_size:
        raise ValueError("syncweb document changed while downloading " + path)
    return actual_hash


def load_collections(binary, data_dir, owner, folder):
    """Load the named package manifests the source published into the folder.

    syncweb stores every collection head and manifest in the folder's document
    store, so a peer that joined the folder can read them without an
    out-of-band manifest ticket. The manifest is fetched through the same
    content-addressed store as the content: syncweb checks the blob against the
    hash recorded by the collection head.
    """
    heads = collection_listing(
        binary, data_dir, owner, folder, "--path-glob", COLLECTION_ROOT + "/*/head"
    )
    if not heads:
        raise ValueError("the syncweb share publishes no collection manifest")
    manifests = collection_listing(
        binary,
        data_dir,
        owner,
        folder,
        "--path-glob",
        COLLECTION_ROOT + "/*/manifests/*",
    )
    candidates = []
    package_names = set()
    for path in sorted(heads):
        collection_id = path.split("/")[1]
        download_document_entry(binary, data_dir, owner, folder, path)
        head_file = folder / path
        if not head_file.is_file():
            raise FileNotFoundError("syncweb did not provide " + path)
        with head_file.open() as head_stream:
            head = json.load(head_stream)
        manifest_hash = head.get("manifest")
        if head.get("collection_id") != collection_id or not isinstance(manifest_hash, str):
            raise ValueError("invalid collection head " + path)
        manifest_path = None
        for candidate_path in sorted(manifests):
            if candidate_path.startswith(
                COLLECTION_ROOT + "/" + collection_id + "/manifests/"
            ) and manifests[candidate_path].get("hash") == manifest_hash:
                manifest_path = candidate_path
        if manifest_path is None:
            raise ValueError(
                "collection " + collection_id + " has no published manifest " + manifest_hash
            )
        download_document_entry(binary, data_dir, owner, folder, manifest_path, manifest_hash)
        manifest_file = folder / manifest_path
        if not manifest_file.is_file():
            raise FileNotFoundError("syncweb did not provide " + manifest_path)
        with manifest_file.open() as manifest_stream:
            manifest = json.load(manifest_stream)
        if manifest.get("schema_version") != 1:
            raise ValueError("unsupported syncweb collection manifest")
        if manifest.get("collection_id") != collection_id:
            raise ValueError("collection manifest does not match its head")
        package = manifest.get("package")
        package_name = package.get("name") if isinstance(package, dict) else None
        if not safe_package_name(package_name):
            raise ValueError("collection " + collection_id + " has no safe package name")
        if package_name in package_names:
            raise ValueError("the share publishes duplicate package name " + package_name)
        package_names.add(package_name)
        candidates.append(
            {
                "id": collection_id,
                "hash": manifest_hash,
                "version": str(manifest.get("version", "")),
                "name": package_name,
                "entries": collection_entries(manifest),
            }
        )
        print("package " + package_name + " manifest blake3: " + manifest_hash)
    return candidates


def package_for_item(collections, package_name, label):
    matches = [collection for collection in collections if collection["name"] == package_name]
    if not matches:
        raise ValueError("the syncweb share publishes no package " + package_name + " for " + label)
    if len(matches) != 1:
        raise ValueError("the syncweb share publishes duplicate package " + package_name)
    return matches[0]


def package_entry_path(package_name, logical_path):
    if not safe_package_name(package_name) or not safe_relative_path(logical_path):
        raise ValueError("invalid package path")
    return package_name + "/" + logical_path


def collection_entry(entries, path, label):
    entry = entries.get(path)
    if entry is None:
        raise ValueError("the collection manifest does not list " + label + " (" + path + ")")
    return entry


def check_staged_file(path, entry, label):
    # syncweb verifies the content-addressed transfer, while the listing checks
    # above bind the selected path to this manifest entry.
    if not path.is_file():
        raise FileNotFoundError("syncweb did not provide " + label)
    if path.stat().st_size != entry["size"]:
        raise ValueError("size mismatch for " + label)


def zim_installed(spec, file_ref):
    # File references are versioned names, so a glob catches the .zim and any
    # .zimaa split part that a previous run left in the content directory.
    return bool(glob.glob(str(Path(spec["zim_content_dir"]) / (file_ref + ".zim*"))))


def module_installed(spec, moddir):
    return (Path(spec["modules_dir"]) / moddir).is_dir()


def cleanup_marker_path(spec_path):
    return spec_path.with_suffix(".folder")


def write_cleanup_marker(marker_path, folder):
    temporary_path = marker_path.with_name(
        marker_path.name + ".tmp-" + str(os.getpid())
    )
    fd = os.open(temporary_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as marker_stream:
            marker_stream.write(str(folder))
        os.replace(temporary_path, marker_path)
    except BaseException:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
        raise


def remove_cleanup_marker(marker_path):
    try:
        marker_path.unlink()
    except FileNotFoundError:
        pass


def install_zim(spec, file_ref, collected, staging_dir, script_dir, remote_path):
    if not safe_name(file_ref):
        raise ValueError("invalid ZIM file reference")

    source = staging_dir / remote_path
    check_staged_file(source, collected, remote_path)

    # zim_install_move.sh picks the file up from the zim working dir, moves it
    # to /library/zims/content and then refreshes the kiwix library and the
    # menus (zim_install_step3.sh cannot be used here: it requires an index
    # in /library/zims/index that IIAB does not maintain)
    staged_zim = Path(spec["zim_working_dir"]) / (file_ref + ".zim")
    shutil.copy2(source, staged_zim)

    subprocess.run(
        [str(script_dir / "zim_install_move.sh"), staged_zim.name],
        check=True,
    )


def install_module(spec, moddir, collected, staging_dir, script_dir, package_name):
    if not safe_name(moddir):
        raise ValueError("invalid module directory")
    if not collected:
        raise ValueError("collection manifest has no files for module " + moddir)

    for file_entry in collected:
        path = file_entry["logical_path"]
        if not safe_relative_path(path):
            raise ValueError("collection manifest path escapes module " + moddir)

    module_working = Path(spec["rachel_working_dir"]) / moddir
    if module_working.exists():
        shutil.rmtree(module_working)
    module_working.mkdir(parents=True)
    for file_entry in collected:
        # empty directories cannot be described by a collection manifest
        logical_path = file_entry["logical_path"]
        remote_path = package_entry_path(package_name, logical_path)
        source = staging_dir / remote_path
        check_staged_file(source, file_entry, logical_path)
        destination = module_working / logical_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)

    subprocess.run(
        [sys.executable, str(script_dir / "oer2go_install_step2.py"), moddir],
        check=True,
    )


def main():
    if sys.version_info < (3, 9):
        # run_syncweb relies on subprocess user=/group=/extra_groups=
        raise SystemExit("iiab-syncweb-fetch.py requires Python 3.9 or later (Debian 11+)")

    parser = argparse.ArgumentParser(description="Fetch selected IIAB content over syncweb.")
    parser.add_argument("--spec-file", required=True)
    parser.add_argument("--binary", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--staging-dir", required=True)
    args = parser.parse_args()

    spec_path = Path(args.spec_file)
    owner = pwd.getpwnam(args.owner)
    staging_root = Path(args.staging_dir)
    script_dir = Path(__file__).resolve().parent
    staging_dir = None
    sync_folder = None
    cleanup_marker = cleanup_marker_path(spec_path)
    cleanup_marker_written = False
    succeeded = False
    cleanup_error = None

    try:
        require_daemon_service(SYNCWEB_SERVICE)

        staging_root.mkdir(parents=True, exist_ok=True)
        staging_dir = Path(tempfile.mkdtemp(prefix="fetch-", dir=staging_root))
        set_owner(staging_dir, owner)
        sync_folder = staging_dir / "folder"

        with spec_path.open() as spec_stream:
            spec = json.load(spec_stream)
        ticket = spec["ticket"]
        if not isinstance(ticket, str) or not ticket.strip():
            raise ValueError("missing syncweb ticket")

        sync_folder.mkdir(mode=0o770)
        set_owner(sync_folder, owner)
        write_cleanup_marker(cleanup_marker, sync_folder)
        cleanup_marker_written = True
        # The ticket goes over standard input ("-"), never in the client's argv,
        # so it is not readable from /proc/<pid>/cmdline.  This needs a syncweb
        # whose `folders join` reads the ticket from stdin; older releases fail.
        run_syncweb(
            args.binary,
            args.data_dir,
            owner,
            "folders",
            "join",
            "-",
            str(sync_folder),
            "--metadata-only",
            "--sync-prefix",
            "collections",
            "--download-existing",
            stdin=ticket.encode(),
        )

        collections = load_collections(args.binary, args.data_dir, owner, sync_folder)

        for entry in spec.get("zims", []):
            file_ref = entry["file_ref"]
            manifest_path = file_ref + ".zim"
            package_name = ZIM_PREFIX + manifest_path
            collection = package_for_item(collections, package_name, "ZIM " + file_ref)
            if set(collection["entries"]) != {manifest_path}:
                raise ValueError("package " + package_name + " is not a single-ZIM package")
            collected = collection_entry(collection["entries"], manifest_path, "ZIM " + file_ref)
            # Check the installed set before downloading: re-running a job that
            # already finished one ZIM must not re-transfer it.
            if zim_installed(spec, file_ref):
                print("ZIM already installed: " + file_ref)
                continue
            download_package_entries(
                args.binary,
                args.data_dir,
                owner,
                sync_folder,
                package_name,
                {package_name: collected},
            )
            install_zim(spec, file_ref, collected, sync_folder, script_dir, package_name)

        for entry in spec.get("modules", []):
            moddir = entry["moddir"]
            package_name = MODULE_PREFIX + moddir
            collection = package_for_item(collections, package_name, "module " + moddir)
            collected = list(collection["entries"].values())
            if not collected:
                raise ValueError("package " + package_name + " contains no files for module " + moddir)
            if module_installed(spec, moddir):
                print("Module already installed: " + moddir)
                continue
            remote_entries = {
                package_entry_path(package_name, item["logical_path"]): item
                for item in collected
            }
            download_package_entries(
                args.binary,
                args.data_dir,
                owner,
                sync_folder,
                package_name,
                remote_entries,
            )
            install_module(spec, moddir, collected, sync_folder, script_dir, package_name)
        succeeded = True
    finally:
        # Clean up unconditionally, but never let cleanup mask the original
        # error.
        original_error = sys.exc_info()[1]
        leave_completed = False
        if cleanup_marker_written:
            try:
                run_syncweb(
                    args.binary,
                    args.data_dir,
                    owner,
                    "folders",
                    "leave",
                    str(sync_folder),
                )
                leave_completed = True
            except (OSError, subprocess.SubprocessError) as exc:
                cleanup_error = exc
                succeeded = False
                print("warning: syncweb folders leave failed: " + str(exc), file=sys.stderr)
            except RuntimeError as exc:
                cleanup_error = exc
                succeeded = False
                print("warning: syncweb folders leave failed: " + str(exc), file=sys.stderr)
        if leave_completed:
            try:
                remove_cleanup_marker(cleanup_marker)
            except OSError as exc:
                cleanup_error = cleanup_error or exc
                print("warning: unable to remove syncweb cleanup marker: " + str(exc), file=sys.stderr)
        if staging_dir is not None:
            if succeeded:
                shutil.rmtree(staging_dir, ignore_errors=True)
            else:
                # keep fully or partially downloaded content for resume/
                # inspection; syncweb's blob store under the data dir also
                # keeps it
                print("keeping staging directory for inspection: " + str(staging_dir), file=sys.stderr)
        try:
            spec_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            cleanup_error = cleanup_error or exc
            print("warning: unable to remove syncweb job spec: " + str(exc), file=sys.stderr)
        if cleanup_error is not None and original_error is None:
            raise RuntimeError("syncweb cleanup failed") from cleanup_error


if __name__ == "__main__":
    main()
