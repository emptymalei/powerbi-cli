"""
Script to capture TUI screenshots with sample data
"""
import asyncio
import json
from pathlib import Path
from pbi_cli.tui import PowerBITUI

# Create sample profile data
def setup_sample_data():
    """Create sample profiles and config for screenshots"""
    config_dir = Path.home() / ".pbi_cli"
    config_dir.mkdir(parents=True, exist_ok=True)
    
    # Create sample profiles
    profiles_file = config_dir / "profiles.yaml"
    profiles_data = {
        "active_profile": "production",
        "profiles": {
            "production": {
                "tenant_id": "12345678-1234-1234-1234-123456789abc",
                "client_id": "87654321-4321-4321-4321-cba987654321",
                "storage": "keyring"
            },
            "development": {
                "tenant_id": "abcdef12-3456-7890-abcd-ef1234567890",
                "client_id": "98765432-8765-4321-9876-543210fedcba",
                "storage": "file"
            }
        }
    }
    
    import yaml
    with open(profiles_file, 'w') as f:
        yaml.dump(profiles_data, f)
    
    # Create sample config
    config_file = config_dir / "config.yaml"
    config_data = {
        "default_output_folder": "~/PowerBI/backups",
        "cache_enabled": True,
        "cache_folder": "~/.pbi_cli/cache"
    }
    
    with open(config_file, 'w') as f:
        yaml.dump(config_data, f)
    
    print("✓ Created sample profiles and config")

async def capture_all_screenshots():
    """Capture screenshots for all major screens"""
    app = PowerBITUI()
    
    async with app.run_test() as pilot:
        # Wait for app to initialize
        await pilot.pause(3)
        
        # 1. Welcome screen
        app.save_screenshot('docs/images/tui_01_welcome.svg')
        print('✓ Captured: Welcome screen')
        
        # 2. Settings & Profiles
        await pilot.press('1')
        await pilot.pause(2)
        app.save_screenshot('docs/images/tui_02_settings_profiles.svg')
        print('✓ Captured: Settings & Profiles')
        
        # Back to main
        await pilot.press('escape')
        await pilot.pause(1)
        
        # 3. Workspaces
        await pilot.press('2')
        await pilot.pause(2)
        app.save_screenshot('docs/images/tui_03_workspaces.svg')
        print('✓ Captured: Workspaces')
        
        await pilot.press('escape')
        await pilot.pause(1)
        
        # 4. Apps
        await pilot.press('3')
        await pilot.pause(2)
        app.save_screenshot('docs/images/tui_04_apps.svg')
        print('✓ Captured: Apps')
        
        await pilot.press('escape')
        await pilot.pause(1)
        
        # 5. Reports
        await pilot.press('4')
        await pilot.pause(2)
        app.save_screenshot('docs/images/tui_05_reports.svg')
        print('✓ Captured: Reports')
        
        await pilot.press('escape')
        await pilot.pause(1)
        
        # 6. Users
        await pilot.press('5')
        await pilot.pause(2)
        app.save_screenshot('docs/images/tui_06_users.svg')
        print('✓ Captured: Users')
        
        await pilot.press('escape')
        await pilot.pause(1)
        
        # 7. Help (button 6)
        await pilot.press('6')
        await pilot.pause(2)
        app.save_screenshot('docs/images/tui_07_help.svg')
        print('✓ Captured: Help')

if __name__ == "__main__":
    print("Setting up sample data...")
    setup_sample_data()
    print("\nCapturing screenshots...")
    asyncio.run(capture_all_screenshots())
    print("\n✅ All 7 screenshots captured successfully!")
    print("\nScreenshots show:")
    print("  - Profile selector with 'production' and 'development' profiles")
    print("  - Output folder: ~/PowerBI/backups")
    print("  - All functional buttons and controls")
    print("  - Sample data in each screen")
