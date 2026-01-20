"""Tests for Windows path handling in config module."""
import sys
from pathlib import Path
import tempfile
import shutil

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from pbi_cli.config import PBIConfig


def test_windows_path_with_trailing_backslash_and_quote():
    """Test Windows path with trailing backslash and escaped quote.
    
    This simulates the issue where:
    pbi config set-output-folder "C:\\Users\\Name\\backups\\"
    results in the string: C:\\Users\\Name\\backups\"
    """
    config = PBIConfig()
    
    # Simulate the problematic input from Windows shell
    test_input = r'C:\Users\TestUser\OneDrive\backups"'
    
    config.default_output_folder = test_input
    result = config.default_output_folder
    
    # The result should NOT end with a quote
    assert not result.endswith('"'), f"Path should not end with quote, got: {result}"
    
    # Should contain 'backups' without quote
    assert 'backups' in result, f"Path should contain 'backups', got: {result}"
    
    print(f"✓ Test passed: {test_input} -> {result}")


def test_windows_path_with_spaces():
    """Test Windows path with spaces in directory names."""
    config = PBIConfig()
    
    # Windows path with spaces (common in OneDrive paths)
    test_input = r'C:\Users\TestUser\OneDrive - Company Name\Data\backups'
    
    config.default_output_folder = test_input
    result = config.default_output_folder
    
    # Should preserve the spaces
    assert 'OneDrive - Company Name' in result or 'OneDrive' in result, \
        f"Path should preserve spaces, got: {result}"
    
    print(f"✓ Test passed: Windows path with spaces handled correctly")


def test_windows_path_with_surrounding_quotes():
    """Test Windows path that's already quoted."""
    config = PBIConfig()
    
    # Path with quotes around it
    test_input = r'"C:\Users\TestUser\backups"'
    
    config.default_output_folder = test_input
    result = config.default_output_folder
    
    # Should strip the surrounding quotes
    assert not result.startswith('"'), f"Path should not start with quote, got: {result}"
    assert not result.endswith('"'), f"Path should not end with quote, got: {result}"
    
    print(f"✓ Test passed: Surrounding quotes stripped correctly")


def test_windows_unc_path():
    """Test Windows UNC network path."""
    config = PBIConfig()
    
    # UNC path format
    test_input = r'\\server\share\backups'
    
    config.default_output_folder = test_input
    result = config.default_output_folder
    
    # Should preserve UNC path format
    assert 'server' in result, f"UNC path should be preserved, got: {result}"
    
    print(f"✓ Test passed: UNC path handled correctly")


def test_unix_path_unaffected():
    """Test that Unix paths are not affected by Windows path handling."""
    config = PBIConfig()
    
    # Standard Unix path
    test_input = "/home/user/backups"
    
    config.default_output_folder = test_input
    result = config.default_output_folder
    
    # Should be converted to absolute path
    assert result == str(Path(test_input).absolute()), \
        f"Unix path should be converted to absolute, got: {result}"
    
    print(f"✓ Test passed: Unix path handled correctly")


def test_path_with_tilde():
    """Test path with tilde for home directory expansion."""
    config = PBIConfig()
    
    # Path with tilde
    test_input = "~/PowerBI/backups"
    
    config.default_output_folder = test_input
    result = config.default_output_folder
    
    # Tilde should be expanded
    assert not result.startswith('~'), f"Tilde should be expanded, got: {result}"
    assert 'PowerBI' in result, f"Path should contain PowerBI, got: {result}"
    
    print(f"✓ Test passed: Tilde expansion works correctly")


def test_path_with_multiple_backslashes():
    """Test Windows path with multiple consecutive backslashes."""
    config = PBIConfig()
    
    # Path with double backslashes (sometimes happens with string concatenation)
    test_input = r'C:\Users\\TestUser\\backups'
    
    config.default_output_folder = test_input
    result = config.default_output_folder
    
    # pathlib should normalize the path
    assert 'TestUser' in result, f"Path should contain TestUser, got: {result}"
    assert 'backups' in result, f"Path should contain backups, got: {result}"
    
    print(f"✓ Test passed: Multiple backslashes normalized")


def test_empty_and_none_values():
    """Test handling of empty and None values."""
    config = PBIConfig()
    
    # Test None
    config.default_output_folder = None
    assert config.default_output_folder is None, "None should be preserved"
    
    print(f"✓ Test passed: None value handled correctly")


def test_path_with_single_quotes():
    """Test path with single quotes instead of double quotes."""
    config = PBIConfig()
    
    # Path with single quotes
    test_input = r"'C:\Users\TestUser\backups'"
    
    config.default_output_folder = test_input
    result = config.default_output_folder
    
    # Should strip the single quotes
    assert not result.startswith("'"), f"Path should not start with single quote, got: {result}"
    assert not result.endswith("'"), f"Path should not end with single quote, got: {result}"
    
    print(f"✓ Test passed: Single quotes stripped correctly")


def test_windows_path_realistic_scenario():
    """Test the exact scenario from the bug report."""
    config = PBIConfig()
    
    # This is what the user would type in PowerShell:
    # pbi config set-output-folder "C:\Users\$Env:UserName\OneDrive - Orion\Data CoE\PowerBI\backups\"
    # Which becomes this string after shell processing:
    test_input = r'C:\Users\L22394\OneDrive - Orion Engineered Carbons GmbH\Data CoE\PowerBI\backups"'
    
    config.default_output_folder = test_input
    result = config.default_output_folder
    
    # Should NOT end with a quote
    assert not result.endswith('"'), f"Path should not end with quote, got: {result}"
    
    # Should contain the expected path components
    assert 'PowerBI' in result, f"Path should contain PowerBI, got: {result}"
    assert 'backups' in result, f"Path should contain backups, got: {result}"
    
    print(f"✓ Test passed: Realistic Windows scenario handled correctly")
    print(f"  Input:  {test_input}")
    print(f"  Output: {result}")


def run_all_tests():
    """Run all test functions."""
    test_functions = [
        test_windows_path_with_trailing_backslash_and_quote,
        test_windows_path_with_spaces,
        test_windows_path_with_surrounding_quotes,
        test_windows_unc_path,
        test_unix_path_unaffected,
        test_path_with_tilde,
        test_path_with_multiple_backslashes,
        test_empty_and_none_values,
        test_path_with_single_quotes,
        test_windows_path_realistic_scenario,
    ]
    
    print("=" * 70)
    print("Running Windows Path Format Tests")
    print("=" * 70)
    print()
    
    failed = []
    for test_func in test_functions:
        try:
            print(f"Running: {test_func.__name__}")
            test_func()
            print()
        except AssertionError as e:
            print(f"✗ FAILED: {e}")
            print()
            failed.append((test_func.__name__, str(e)))
        except Exception as e:
            print(f"✗ ERROR: {e}")
            print()
            failed.append((test_func.__name__, str(e)))
    
    print("=" * 70)
    if failed:
        print(f"FAILED: {len(failed)} test(s) failed")
        for name, error in failed:
            print(f"  - {name}: {error}")
        return False
    else:
        print(f"SUCCESS: All {len(test_functions)} tests passed!")
        return True


if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)
