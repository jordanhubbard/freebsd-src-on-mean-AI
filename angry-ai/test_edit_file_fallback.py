#!/usr/bin/env python3
"""
Test EDIT_FILE fallback parser for when LLM uses <<< as closing delimiter.
"""

import re

def parse_edit_file_with_fallback(body: str):
    """
    Parse EDIT_FILE blocks with fallback for <<< misuse.
    
    Returns: (old_str, new_str, warnings)
    """
    warnings = []
    
    # Try standard format first: OLD:<<<...>>>
    old_match = re.search(r'OLD:\s*<<<(.*?)>>>', body, re.DOTALL)
    
    # FALLBACK: If model used <<< as closing delimiter instead of >>>
    if not old_match and 'NEW:' in body:
        fallback_match = re.search(r'OLD:\s*<<<(.*?)(?:<<<\s*)?NEW:', body, re.DOTALL)
        if fallback_match:
            old_match = fallback_match
            warnings.append("OLD block: Model used <<< as closing delimiter instead of >>>")
    
    if not old_match:
        raise ValueError("Could not find OLD block")
    
    old_str = old_match.group(1).strip()
    
    # Try standard format for NEW: NEW:<<<...>>>
    new_match = re.search(r'NEW:\s*<<<(.*?)>>>', body, re.DOTALL)
    
    # FALLBACK: If model used <<< as closing delimiter
    if not new_match:
        fallback_match = re.search(r'NEW:\s*<<<(.*?)(?:<<<\s*)?$', body, re.DOTALL)
        if fallback_match:
            new_match = fallback_match
            warnings.append("NEW block: Model used <<< as closing delimiter instead of >>>")
    
    if not new_match:
        raise ValueError("Could not find NEW block")
    
    new_str = new_match.group(1).strip()
    
    return old_str, new_str, warnings


# Test cases
test_cases = [
    {
        "name": "Correct format (>>> delimiters)",
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
        "should_work": True,
        "expected_warnings": 0,
    },
    
    {
        "name": "Malformed: <<< used as closing delimiter (user's actual error)",
        "body": """OLD:
<<<
786:            if (type != LT_CLASS) {
787:                    errno = 0;
788:                    lval = strtol(sp, &ep, 0);
789:                    if (errno == ERANGE)
790:                            errx(STATUS_BADUSAGE,
<<<
NEW:
<<<
786:            if (type != LT_CLASS) {
787:                    errno = 0;
788:                    lval = strtol(sp, &ep, 0);
789:                    if (errno == ERANGE)
790:                            errx(STATUS_BADUSAGE, "Invalid numeric value: %s", sp);
<<<""",
        "should_work": True,
        "expected_warnings": 2,  # Both OLD and NEW blocks malformed
    },
    
    {
        "name": "Partially malformed: OLD correct, NEW wrong",
        "body": """OLD:
<<<
some content
>>>
NEW:
<<<
new content
<<<""",
        "should_work": True,
        "expected_warnings": 1,  # Only NEW block malformed
    },
    
    {
        "name": "Both blocks correct with extra whitespace",
        "body": """OLD:  <<<
content
  >>>
NEW:   <<<
new content
>>>""",
        "should_work": True,
        "expected_warnings": 0,
    },
]


def run_tests():
    """Run all test cases"""
    print("Testing EDIT_FILE fallback parser\n")
    print("=" * 70)
    
    passed = 0
    failed = 0
    
    for i, test in enumerate(test_cases, 1):
        print(f"\nTest {i}: {test['name']}")
        print("-" * 70)
        
        body = test['body']
        
        try:
            old_str, new_str, warnings = parse_edit_file_with_fallback(body)
            
            # Check if parsing succeeded
            if not test['should_work']:
                print("✗ FAIL: Parsing succeeded but should have failed")
                failed += 1
                continue
            
            # Check warning count
            if len(warnings) != test['expected_warnings']:
                print(f"✗ FAIL: Expected {test['expected_warnings']} warnings, got {len(warnings)}")
                failed += 1
                continue
            
            print(f"✓ PASS")
            print(f"  OLD: {repr(old_str[:60])}{'...' if len(old_str) > 60 else ''}")
            print(f"  NEW: {repr(new_str[:60])}{'...' if len(new_str) > 60 else ''}")
            
            if warnings:
                print(f"  Warnings: {len(warnings)}")
                for w in warnings:
                    print(f"    - {w}")
            
            passed += 1
            
        except Exception as e:
            if test['should_work']:
                print(f"✗ FAIL: {e}")
                failed += 1
            else:
                print(f"✓ PASS: Correctly failed with: {e}")
                passed += 1
    
    print("\n" + "=" * 70)
    print(f"\nResults: {passed} passed, {failed} failed")
    
    return failed == 0


if __name__ == "__main__":
    import sys
    success = run_tests()
    sys.exit(0 if success else 1)
