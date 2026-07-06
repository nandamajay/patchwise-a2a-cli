"""Tests for a2a_cli.kernel_manager module."""
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from a2a_cli.kernel_manager import (
    managed_kernel_path,
    patches_already_applied,
    apply_patches,
    prepare_kernel_with_patches,
)


def test_managed_kernel_path():
    root = Path("/tmp/test-a2a")
    result = managed_kernel_path(root)
    assert result == Path("/tmp/test-a2a/.a2a/kernel/linux-next")


def test_patches_already_applied_all_reverse(tmp_path):
    """If all patches reverse-apply cleanly, they're already applied."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "--allow-empty", "-m", "init"], capture_output=True, check=True)

    # Create a file and commit it
    (repo / "test.c").write_text("hello\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "add test"], capture_output=True, check=True)

    # Create a patch that adds a line
    patch_file = tmp_path / "0001-test.patch"
    patch_content = """\
From 0000000000000000000000000000000000000000 Mon Sep 17 00:00:00 2001
From: Test <test@test.com>
Date: Mon, 1 Jan 2024 00:00:00 +0000
Subject: [PATCH] add line

---
 test.c | 1 +
 1 file changed, 1 insertion(+)

diff --git a/test.c b/test.c
index ce01362..a042389 100644
--- a/test.c
+++ b/test.c
@@ -1 +1,2 @@
 hello
+world
-- 
2.34.1
"""
    patch_file.write_text(patch_content)

    # Apply the patch first
    subprocess.run(
        ["git", "-C", str(repo), "am", str(patch_file)],
        capture_output=True, check=True,
    )

    # Now check — should detect as already applied
    assert patches_already_applied(repo, [patch_file]) is True


def test_patches_not_applied(tmp_path):
    """If patches don't reverse, they're not applied."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], capture_output=True, check=True)
    (repo / "test.c").write_text("hello\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], capture_output=True, check=True)

    patch_file = tmp_path / "0001-test.patch"
    patch_content = """\
From 0000000000000000000000000000000000000000 Mon Sep 17 00:00:00 2001
From: Test <test@test.com>
Date: Mon, 1 Jan 2024 00:00:00 +0000
Subject: [PATCH] add line

---
 test.c | 1 +
 1 file changed, 1 insertion(+)

diff --git a/test.c b/test.c
index ce01362..a042389 100644
--- a/test.c
+++ b/test.c
@@ -1 +1,2 @@
 hello
+world
-- 
2.34.1
"""
    patch_file.write_text(patch_content)

    # Not applied yet
    assert patches_already_applied(repo, [patch_file]) is False


def test_apply_patches_success(tmp_path):
    """Patches should apply cleanly on a compatible base."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t.com"], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], capture_output=True, check=True)
    (repo / "test.c").write_text("hello\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], capture_output=True, check=True)

    patch_file = tmp_path / "0001-test.patch"
    patch_content = """\
From 0000000000000000000000000000000000000000 Mon Sep 17 00:00:00 2001
From: Test <test@test.com>
Date: Mon, 1 Jan 2024 00:00:00 +0000
Subject: [PATCH] add line

---
 test.c | 1 +
 1 file changed, 1 insertion(+)

diff --git a/test.c b/test.c
index ce01362..a042389 100644
--- a/test.c
+++ b/test.c
@@ -1 +1,2 @@
 hello
+world
-- 
2.34.1
"""
    patch_file.write_text(patch_content)

    result = apply_patches(repo, [patch_file])
    assert result["ok"] is True
    assert result["applied_count"] == 1
    assert (repo / "test.c").read_text() == "hello\nworld\n"


def test_apply_patches_conflict(tmp_path):
    """Conflicting patches should return ok=False with detail."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t.com"], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], capture_output=True, check=True)
    (repo / "test.c").write_text("different content\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], capture_output=True, check=True)

    patch_file = tmp_path / "0001-test.patch"
    patch_content = """\
From 0000000000000000000000000000000000000000 Mon Sep 17 00:00:00 2001
From: Test <test@test.com>
Date: Mon, 1 Jan 2024 00:00:00 +0000
Subject: [PATCH] add line

---
 test.c | 1 +
 1 file changed, 1 insertion(+)

diff --git a/test.c b/test.c
index ce01362..a042389 100644
--- a/test.c
+++ b/test.c
@@ -1 +1,2 @@
 hello
+world
-- 
2.34.1
"""
    patch_file.write_text(patch_content)

    result = apply_patches(repo, [patch_file])
    assert result["ok"] is False
    assert result["conflict_detail"] != ""


def test_prepare_kernel_with_patches_custom_path_not_found(tmp_path):
    """Non-existent custom path should return ok=False."""
    result = prepare_kernel_with_patches(
        tmp_path, [], kernel_path_override="/nonexistent/path"
    )
    assert result["ok"] is False
    assert "not found" in result["conflict_detail"]
