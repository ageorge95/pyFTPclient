from sys import version_info
import subprocess
import argparse
import platform

# PySide6 requires Python >= 3.9
INVALID_PYTHON_VERSIONS = [
    "3.7",
    "3.8",
]

# Candidate version tags ordered from highest to lowest.
# For each version we try multiple discovery commands suited to the current OS.
PYTHON_VERSION_TAGS = [
    ('python3.14', '314', '3.14'),
    ('python3.13', '313', '3.13'),
    ('python3.12', '312', '3.12'),
    ('python3.11', '311', '3.11'),
    ('python3.10', '310', '3.10'),
    ('python3.9',  '39',  '3.9'),
]


def _build_candidates():
    """
    Build the ordered dict of version_name -> list-of-commands appropriate
    for the current operating system.
    """
    is_windows = platform.system() == "Windows"
    candidates = {}
    for version_name, win_suffix, ver in PYTHON_VERSION_TAGS:
        if is_windows:
            candidates[version_name] = [
                f'where python{win_suffix}',
                f'py -{ver} -c "import sys; print(sys.executable)"',
            ]
        else:
            # On Linux/macOS: try the versioned binary directly, then 'which'
            candidates[version_name] = [
                f'python{ver} -c "import sys; print(sys.executable)"',
                f'which python{ver}',
            ]
    return candidates


PYTHON_INSTALL_CANDIDATES = _build_candidates()


def check_current_python_version():
    """
    Checks if the currently running Python version is in the INVALID_PYTHON_VERSIONS list.
    Raises an Exception if it's invalid, otherwise prints a success message.
    """
    print("--- Checking Current Python Version ---")
    current_python_version_short = f'{version_info.major}.{version_info.minor}'
    current_python_version_long = f'{version_info.major}.{version_info.minor}.{version_info.micro}'

    print(f"Currently running Python: {current_python_version_long}")

    if (current_python_version_short in INVALID_PYTHON_VERSIONS) or \
       (current_python_version_long in INVALID_PYTHON_VERSIONS):
        raise Exception(
            f'Invalid python version. Current: {current_python_version_short} (or {current_python_version_long}). '
            f'Invalid list: {", ".join(INVALID_PYTHON_VERSIONS)}'
        )
    else:
        print('Your current Python version seems fine.')


def find_highest_installed_python():
    """
    Searches for installed Python versions from a predefined list,
    ordered from highest to lowest, and returns the path to the first one found.
    Works on both Windows and Linux/macOS.
    """
    print(f"\n--- Finding Highest Installed Python Version (platform: {platform.system()}) ---")
    python_found_path = None
    python_found_version_name = None

    # Iterate in the order defined in PYTHON_INSTALL_CANDIDATES
    for version_name, possible_commands in PYTHON_INSTALL_CANDIDATES.items():
        # Extract the dotted version string (e.g. "3.12") from the name and skip if invalid
        dotted_ver = version_name.replace("python", "")
        if dotted_ver in INVALID_PYTHON_VERSIONS:
            print(f"Skipping {version_name} (in invalid versions list).")
            continue
        print(f"Checking for {version_name}...")
        for command in possible_commands:
            try:
                call_result = subprocess.check_output(
                    command, shell=True, text=True, stderr=subprocess.DEVNULL
                ).strip()

                # Take only the first line (e.g. 'where' may return multiple hits)
                call_result = call_result.splitlines()[0].strip() if call_result else ""

                if call_result:  # Make sure we got a non-empty path
                    print(f"  SUCCESS: Found {version_name} using command '{command}': {call_result}")
                    python_found_version_name = version_name
                    python_found_path = call_result
                    break  # Found this version, no need to check other commands for it
            except subprocess.CalledProcessError:
                print(f"  INFO: Command '{command}' failed or returned non-zero for {version_name}.")
            except FileNotFoundError:
                print(f"  INFO: Command '{command.split()[0]}' not found while checking for {version_name}.")

        if python_found_path:
            print(f"Highest version found so far: {python_found_version_name} at {python_found_path}")
            break

    if python_found_path:
        print(f"\nConclusion: The highest Python version found from the list is: {python_found_version_name}")
        print(f"Path: {python_found_path}")
        return python_found_version_name, python_found_path
    else:
        print("\nConclusion: No Python version from the predefined list was found on this system.")
        return None, None


# --- Main execution logic with argparse ---
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Python version utility script.",
        formatter_class=argparse.RawTextHelpFormatter  # For better help text formatting
    )
    parser.add_argument(
        "action",
        nargs="?",
        default="check_current",
        choices=["check_current", "find_highest", "all"],
        help=(
            "Specify the action to perform (default: check_current):\n"
            "  check_current: Validates the Python version this script is running with.\n"
            "  find_highest:  Finds the highest available Python version from a predefined list on your system.\n"
            "  all:           Runs both 'check_current' and 'find_highest'."
        )
    )

    args = parser.parse_args()

    if args.action == "check_current" or args.action == "all":
        check_current_python_version()

    if args.action == "find_highest" or args.action == "all":
        find_highest_installed_python()

    print("\nScript finished.")
