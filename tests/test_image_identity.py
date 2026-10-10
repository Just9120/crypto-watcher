"""Archive-to-runtime identity regressions for classic Docker and containerd."""

import hashlib
import io
import json
import tarfile
from unittest.mock import Mock

import pytest

from scripts import deploy_receiver as receiver

SOURCE = "a" * 40
RUN = 123


def digest(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def image_archive(tmp_path, *, oci=True, change=None):
    layer = b"disposable-layer"
    layer_id = digest(layer)
    config = {
        "os": "linux",
        "architecture": "amd64",
        "config": {
            "User": "10001:10001",
            "Labels": {"org.opencontainers.image.revision": SOURCE, "io.cryptowatcher.build-run": str(RUN)},
        },
        "rootfs": {"type": "layers", "diff_ids": [layer_id]},
    }
    config_bytes = json.dumps(config).encode()
    config_id = digest(config_bytes)
    config_path = "blobs/sha256/" + config_id[7:]
    layer_path = "blobs/sha256/" + layer_id[7:]
    oci_manifest = {
        "schemaVersion": 2,
        "config": {"digest": config_id, "size": len(config_bytes)},
        "layers": [{"digest": layer_id, "size": len(layer)}],
    }
    oci_bytes = json.dumps(oci_manifest).encode()
    manifest_id = digest(oci_bytes)
    entries = [{"Config": config_path, "RepoTags": ["cryptowatcher:" + SOURCE], "Layers": [layer_path]}]
    index = {
        "schemaVersion": 2,
        "manifests": [
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": manifest_id,
                "size": len(oci_bytes),
            }
        ],
    }
    files = {"manifest.json": json.dumps(entries).encode(), config_path: config_bytes, layer_path: layer}
    if oci:
        files.update({"index.json": json.dumps(index).encode(), "blobs/sha256/" + manifest_id[7:]: oci_bytes})
    if change:
        change(files, config_path)
    path = tmp_path / "image.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    release = {"source_sha": SOURCE, "build_run": RUN, "architecture": "amd64", "image_id": config_id}
    return path, release, config_id, manifest_id, config


def inspect_response(image_id, config):
    return {
        "Id": image_id,
        "Os": config["os"],
        "Architecture": config["architecture"],
        "RootFS": {"Layers": config["rootfs"]["diff_ids"]},
        "Config": config["config"],
    }


@pytest.mark.parametrize("build_store", ["classic", "containerd"])
@pytest.mark.parametrize("target_store", ["classic", "containerd"])
def test_same_archive_resolves_exact_identity_across_stores(tmp_path, monkeypatch, build_store, target_store):
    path, release, config_id, manifest_id, config = image_archive(tmp_path)
    release["image_id"] = config_id if build_store == "classic" else manifest_id
    expected = config_id if target_store == "classic" else manifest_id
    identity = receiver.archive_image_identity(path, release)
    docker = Mock(return_value=json.dumps([inspect_response(expected, config)]))
    monkeypatch.setattr(receiver, "run", docker)
    assert receiver.loaded_image_id(identity, release) == expected
    docker.assert_called_once_with(["docker", "image", "inspect", "cryptowatcher:" + SOURCE])
    assert identity["config_digest"] == config_id
    assert identity["manifest_digest"] == manifest_id


def test_legacy_docker_archive_has_only_config_identity(tmp_path, monkeypatch):
    path, release, config_id, manifest_id, config = image_archive(tmp_path, oci=False)
    identity = receiver.archive_image_identity(path, release)
    assert identity["identities"] == {config_id}
    monkeypatch.setattr(receiver, "run", lambda args: json.dumps([inspect_response(manifest_id, config)]))
    with pytest.raises(receiver.DeploymentError, match="differs from verified archive"):
        receiver.loaded_image_id(identity, release)


def test_matching_labels_do_not_allow_a_different_image(tmp_path, monkeypatch):
    path, release, _, _, config = image_archive(tmp_path)
    identity = receiver.archive_image_identity(path, release)
    monkeypatch.setattr(
        receiver, "run", lambda args: json.dumps([inspect_response("sha256:" + "f" * 64, config)])
    )
    with pytest.raises(receiver.DeploymentError, match="differs from verified archive"):
        receiver.loaded_image_id(identity, release)


@pytest.mark.parametrize("field", ["Architecture", "Os", "RootFS", "Config"])
def test_loaded_platform_rootfs_and_execution_config_must_match(tmp_path, monkeypatch, field):
    path, release, config_id, _, config = image_archive(tmp_path)
    identity = receiver.archive_image_identity(path, release)
    info = inspect_response(config_id, config)
    info[field] = {
        "Architecture": "arm64",
        "Os": "windows",
        "RootFS": {"Layers": ["sha256:" + "b" * 64]},
        "Config": {"User": "0", "Labels": config["config"]["Labels"]},
    }[field]
    monkeypatch.setattr(receiver, "run", lambda args: json.dumps([info]))
    with pytest.raises(receiver.DeploymentError):
        receiver.loaded_image_id(identity, release)


def test_oci_metadata_tampering_is_rejected(tmp_path):
    def change(files, config_path):
        index = json.loads(files["index.json"])
        name = "blobs/sha256/" + index["manifests"][0]["digest"][7:]
        files[name] += b" "

    path, release, *_ = image_archive(tmp_path, change=change)
    with pytest.raises(receiver.DeploymentError, match="OCI manifest digest mismatch"):
        receiver.archive_image_identity(path, release)


def test_config_bytes_cannot_disagree_with_content_address(tmp_path):
    def change(files, config_path):
        files[config_path] += b" "

    path, release, *_ = image_archive(tmp_path, change=change)
    with pytest.raises(receiver.DeploymentError, match="config digest mismatch"):
        receiver.archive_image_identity(path, release)


@pytest.mark.parametrize("change", ["tag", "multiple", "build", "provenance"])
def test_ambiguous_or_inconsistent_provenance_is_rejected(tmp_path, change):
    def alter(files, config_path):
        if change == "tag":
            entries = json.loads(files["manifest.json"])
            entries[0]["RepoTags"] = ["cryptowatcher:latest"]
            files["manifest.json"] = json.dumps(entries).encode()
        if change == "multiple":
            index = json.loads(files["index.json"])
            index["manifests"] *= 2
            files["index.json"] = json.dumps(index).encode()

    path, release, *_ = image_archive(tmp_path, change=alter)
    if change == "build":
        release["build_run"] = RUN + 1
    if change == "provenance":
        release["image_config_digest"] = "sha256:" + "f" * 64
    with pytest.raises(receiver.DeploymentError):
        receiver.archive_image_identity(path, release)
