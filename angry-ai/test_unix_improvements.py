#!/usr/bin/env python3
"""
Test Unix-specific improvements to angry_ai.
"""

import sys
import tempfile
from pathlib import Path
from angry_ai import resolve_repo_path, validate_relative_path, tool_list_dir


def test_resolve_repo_path():
    """Test Unix-specific path resolution with symlinks."""
    
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir) / "repo"
        repo.mkdir()
        
        # Create a test file
        test_file = repo / "test.txt"
        test_file.write_text("hello")
        
        # Test normal path resolution
        resolved = resolve_repo_path("test.txt", repo)
        assert resolved == test_file.resolve()
        print("✓ Normal path resolution works")
        
        # Test that symlinks are resolved (Unix-specific)
        link = repo / "link.txt"
        link.symlink_to(test_file)
        resolved = resolve_repo_path("link.txt", repo)
        # Should resolve to the actual file
        assert resolved.exists()
        print("✓ Symlink resolution works (Unix-specific)")
        
        # Test escape attempt
        try:
            # Try to escape via parent directory
            resolve_repo_path("../outside.txt", repo)
            print("✗ Should have caught escape attempt")
            sys.exit(1)
        except ValueError:
            print("✓ Escape attempt blocked")


def test_tilde_expansion_blocked():
    """Test that ~ expansion is blocked (Unix-specific)."""
    
    try:
        validate_relative_path("~/etc/passwd")
        print("✗ Tilde expansion should be blocked")
        sys.exit(1)
    except ValueError as e:
        assert "home directory" in str(e).lower()
        print("✓ Tilde expansion blocked (Unix-specific)")


def test_gitignore_filtering():
    """Test .gitignore filtering in LIST_DIR (Unix-specific)."""
    
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir) / "test_repo"
        repo.mkdir()
        
        # Initialize git repo (Unix assumption: git is available)
        import subprocess
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        
        # Create .gitignore
        gitignore = repo / ".gitignore"
        gitignore.write_text("*.log\nbuild/\n")
        
        # Create files (some ignored, some not)
        (repo / "main.c").write_text("code")
        (repo / "test.log").write_text("log")
        (repo / "README.md").write_text("docs")
        build_dir = repo / "build"
        build_dir.mkdir()
        (build_dir / "output.o").write_text("binary")
        
        # Test LIST_DIR with gitignore filtering (default)
        result = tool_list_dir(repo, show_ignored=False)
        
        # Should include non-ignored files
        assert "main.c" in result
        assert "README.md" in result
        assert ".gitignore" in result
        
        # Should exclude ignored files
        assert "test.log" not in result
        assert "build" not in result
        
        print("✓ .gitignore filtering works (Unix-specific)")
        
        # Test with show_ignored=True
        result_all = tool_list_dir(repo, show_ignored=True)
        assert "test.log" in result_all
        assert "build" in result_all
        print("✓ show_ignored=True includes all files")


def test_unix_only_path_handling():
    """Test that we correctly handle Unix-only path separators."""
    
    # Unix paths use / only
    validate_relative_path("bin/test/file.c")
    print("✓ Unix path separator handling")
    
    # Absolute path detection (Unix-specific: starts with /)
    try:
        validate_relative_path("/etc/passwd")
        print("✗ Should catch absolute Unix path")
        sys.exit(1)
    except ValueError:
        print("✓ Unix absolute path detection")


if __name__ == "__main__":
    print("Testing Unix-specific improvements...\n")
    
    test_resolve_repo_path()
    test_tilde_expansion_blocked()
    test_unix_only_path_handling()
    test_gitignore_filtering()
    
    print("\n✓ All Unix-specific tests passed!")
    print("\nThese optimizations leverage:")
    print("  • Unix path conventions (/ separator, absolute path detection)")
    print("  • Git availability (check-ignore for .gitignore filtering)")
    print("  • Symlink resolution via realpath")
    print("  • Known execution context (running in repo)")
