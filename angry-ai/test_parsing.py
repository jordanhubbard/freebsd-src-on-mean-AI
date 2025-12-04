#!/usr/bin/env python3
"""
Test script for ACTION parsing robustness improvements.
"""

import sys
from angry_ai import parse_action, validate_relative_path, strip_markdown_fences


def test_last_action_line():
    """Test that we use the LAST ACTION line, not the first."""
    output = """
I'll read the file first. For example:
   ACTION: READ_FILE example.txt
   
But actually, let me do something different.

ACTION: LIST_DIR bin/
"""
    parsed = parse_action(output)
    assert parsed.action == "LIST_DIR", f"Expected LIST_DIR, got {parsed.action}"
    assert parsed.argument == "bin/", f"Expected 'bin/', got '{parsed.argument}'"
    print("✓ Last ACTION line test passed")


def test_path_validation():
    """Test path validation catches dangerous patterns."""
    
    # Should pass
    try:
        validate_relative_path("bin/pkill/pkill.c")
        validate_relative_path("lib/test.c")
        print("✓ Valid relative paths accepted")
    except ValueError as e:
        print(f"✗ False positive on valid path: {e}")
        sys.exit(1)
    
    # Should fail - absolute path
    try:
        validate_relative_path("/etc/passwd")
        print("✗ Absolute path not caught")
        sys.exit(1)
    except ValueError:
        print("✓ Absolute path rejected")
    
    # Should fail - parent directory escape
    try:
        validate_relative_path("../../../etc/passwd")
        print("✗ Parent directory escape not caught")
        sys.exit(1)
    except ValueError:
        print("✓ Parent directory escape rejected")
    
    # Should fail - sneaky escape
    try:
        validate_relative_path("bin/../../etc/passwd")
        print("✗ Sneaky escape not caught")
        sys.exit(1)
    except ValueError:
        print("✓ Sneaky escape rejected")


def test_lenient_whitespace():
    """Test that we handle whitespace variations."""
    
    # Test with extra spaces and tabs
    output = """
ACTION: EDIT_FILE test.txt
OLD:  
<<<
old text
>>>
NEW:  
  <<<
new text
  >>>
"""
    parsed = parse_action(output)
    assert parsed.action == "EDIT_FILE"
    assert parsed.old_str == "old text"
    assert parsed.new_str == "new text"
    print("✓ Lenient whitespace parsing passed")


def test_markdown_fence_stripping():
    """Test that markdown fences are stripped."""
    
    text = """```python
def hello():
    print("world")
```"""
    
    stripped = strip_markdown_fences(text)
    assert "```" not in stripped, "Fences not stripped"
    assert 'def hello():' in stripped
    print("✓ Markdown fence stripping passed")


def test_edit_file_with_fences():
    """Test EDIT_FILE with markdown fences in blocks."""
    
    output = """
ACTION: EDIT_FILE main.c
OLD:
<<<
```c
int main() {
    return 0;
}
```
>>>
NEW:
<<<
```c
int main() {
    printf("Hello\\n");
    return 0;
}
```
>>>
"""
    parsed = parse_action(output)
    assert parsed.action == "EDIT_FILE"
    # Fences should be stripped
    assert "```c" not in parsed.old_str
    assert "int main()" in parsed.old_str
    print("✓ EDIT_FILE with markdown fences passed")


def test_error_messages():
    """Test that error messages are helpful."""
    
    output = """
ACTION: EDIT_FILE test.txt
OLDD:
<<<
wrong keyword
>>>
"""
    try:
        parse_action(output)
        print("✗ Should have raised ValueError for missing OLD block")
        sys.exit(1)
    except ValueError as e:
        error_msg = str(e)
        # Check that error message shows preview
        assert "preview" in error_msg.lower() or "OLDD" in error_msg
        print(f"✓ Error message is helpful: {error_msg[:100]}...")


if __name__ == "__main__":
    print("Testing ACTION parsing robustness improvements...\n")
    
    test_last_action_line()
    test_path_validation()
    test_lenient_whitespace()
    test_markdown_fence_stripping()
    test_edit_file_with_fences()
    test_error_messages()
    
    print("\n✓ All tests passed!")
