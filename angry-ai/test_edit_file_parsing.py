#!/usr/bin/env python3
"""
Quick test to validate EDIT_FILE parsing fixes.
"""

import re

def strip_markdown_fences(text: str) -> str:
    """Strip markdown code fences from text if present."""
    text = text.strip()
    lines = text.split('\n')
    
    if lines and lines[0].strip().startswith('```'):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith('```'):
        lines = lines[:-1]
    
    return '\n'.join(lines)


def parse_edit_file_old_regex(body: str):
    """OLD (broken) regex - requires mandatory newlines"""
    old_match = re.search(r'OLD:\s*\n?\s*<<<\s*\n(.*?)\n\s*>>>', body, re.DOTALL)
    new_match = re.search(r'NEW:\s*\n?\s*<<<\s*\n(.*?)\n\s*>>>', body, re.DOTALL)
    return old_match, new_match


def parse_edit_file_new_regex(body: str):
    """NEW (fixed) regex - lenient about whitespace"""
    old_match = re.search(r'OLD:\s*<<<(.*?)>>>', body, re.DOTALL)
    new_match = re.search(r'NEW:\s*<<<(.*?)>>>', body, re.DOTALL)
    return old_match, new_match


# Test cases
test_cases = [
    # Test 1: Standard format (with newlines everywhere)
    {
        "name": "Standard format",
        "body": """OLD:
<<<
line1
line2
>>>
NEW:
<<<
line3
line4
>>>""",
        "should_work_old": True,
        "should_work_new": True,
    },
    
    # Test 2: Compact format (no newlines after <<< or before >>>)
    {
        "name": "Compact format",
        "body": "OLD:<<<content>>>NEW:<<<new_content>>>",
        "should_work_old": False,  # OLD regex requires newlines
        "should_work_new": True,   # NEW regex is lenient
    },
    
    # Test 3: Empty content blocks
    {
        "name": "Empty content",
        "body": """OLD:
<<<
>>>
NEW:
<<<
>>>""",
        "should_work_old": False,  # OLD regex can't handle empty content (no newline before >>>)
        "should_work_new": True,   # NEW regex handles this fine
    },
    
    # Test 4: Mixed whitespace
    {
        "name": "Mixed whitespace",
        "body": """OLD:  <<<
  content  
>>>
NEW:   <<<  
  new_content
  >>>""",
        "should_work_old": False,  # OLD regex is strict about newlines
        "should_work_new": True,   # NEW regex is lenient
    },
    
    # Test 5: Content with line numbers (like in the error message)
    {
        "name": "Content with line numbers",
        "body": """OLD:
<<<
209:   if (signal(SIGINFO, siginfo) == SIG_ERR)
210: warn("signal(SIGINFO)");
211: 
>>>
NEW:
<<<
209:   if (signal(SIGINFO, siginfo) == SIG_ERR)
210:     warn("signal(SIGINFO)");
211: 
>>>""",
        "should_work_old": True,
        "should_work_new": True,
    },
]


def run_tests():
    """Run all test cases"""
    print("Testing EDIT_FILE parsing regex fixes\n")
    print("=" * 70)
    
    for i, test in enumerate(test_cases, 1):
        print(f"\nTest {i}: {test['name']}")
        print("-" * 70)
        
        body = test['body']
        
        # Test OLD regex
        old_match, new_match = parse_edit_file_old_regex(body)
        old_works = (old_match is not None) and (new_match is not None)
        
        # Test NEW regex
        old_match_new, new_match_new = parse_edit_file_new_regex(body)
        new_works = (old_match_new is not None) and (new_match_new is not None)
        
        # Expected results
        expected_old = test['should_work_old']
        expected_new = test['should_work_new']
        
        # Check results
        old_result = "✓ PASS" if old_works == expected_old else "✗ FAIL"
        new_result = "✓ PASS" if new_works == expected_new else "✗ FAIL"
        
        print(f"Old regex: {old_result} (expected {'success' if expected_old else 'failure'}, got {'success' if old_works else 'failure'})")
        print(f"New regex: {new_result} (expected {'success' if expected_new else 'failure'}, got {'success' if new_works else 'failure'})")
        
        # Show what was captured (if successful)
        if new_works:
            old_str = old_match_new.group(1).strip()
            new_str = new_match_new.group(1).strip()
            print(f"\nCaptured OLD: {repr(old_str[:50])}{'...' if len(old_str) > 50 else ''}")
            print(f"Captured NEW: {repr(new_str[:50])}{'...' if len(new_str) > 50 else ''}")
    
    print("\n" + "=" * 70)
    print("\nSummary:")
    print("- Old regex: Strict, requires exact newline placement (BROKEN)")
    print("- New regex: Lenient, handles various whitespace patterns (FIXED)")


if __name__ == "__main__":
    run_tests()
