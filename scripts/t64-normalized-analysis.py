#!/usr/bin/env python3
"""
Ubuntu Package t64 and Normalized Name Analysis

Analyze Ubuntu Contents files to answer two specific questions:
1. How many packages have t64 suffix in versions prior to Noble?
2. What .so normalized name changes occurred (not just version increments)?

BEFORE RUNNING: Download Ubuntu Contents Files
-----------------------------------------------
You must first download the Contents files from Ubuntu's archive:

    # Example of Download Contents files for different Ubuntu releases
    wget http://archive.ubuntu.com/ubuntu/dists/focal/main/Contents-amd64.gz
    wget http://archive.ubuntu.com/ubuntu/dists/jammy/main/Contents-amd64.gz
    wget http://archive.ubuntu.com/ubuntu/dists/noble/main/Contents-amd64.gz
    
    # Decompress them
    gunzip Contents-amd64.gz
    mv Contents-amd64 ubuntu-focal.txt
    
    # Repeat for each release, renaming appropriately:
    # ubuntu-focal.txt  (Ubuntu 20.04)
    # ubuntu-jammy.txt  (Ubuntu 22.04)
    # ubuntu-noble.txt  (Ubuntu 24.04)

Available Ubuntu releases:
    - focal  (20.04 LTS)
    - jammy  (22.04 LTS)
    - noble  (24.04 LTS)
    - bionic (18.04 LTS) - older
    
Contents file format: Each line shows a file path and which package provides it
    Example: usr/lib/x86_64-linux-gnu/libssl.so.3    libs/libssl3t64

Usage:
    python t64-normalized-analysis.py ubuntu-jammy.txt ubuntu-noble.txt --output-dir analysis-results/
"""

import argparse
import sys
import os
from pathlib import Path
from typing import Dict, Set, List, Tuple
from collections import defaultdict
import re

# Import normalize function from normalize.py
try:
    from normalize import normalize_file_name, NormalizedFileName
except ImportError:
    print("Error: Could not import normalize.py. Make sure it's in the same directory.")
    sys.exit(1)


def parse_contents_line(line: str) -> Tuple[str, str]:
    """
    Parse a single line from Contents file.
    
    Returns:
        (filepath, package_name) or (None, None) if invalid
    """
    parts = line.strip().split()
    if len(parts) < 2:
        return None, None
    
    filepath = parts[0]
    package_info = parts[-1]  # Last column is always package
    
    # Extract package name (format: "section/package" or "package")
    if '/' in package_info:
        package_name = package_info.split('/')[-1]
    else:
        package_name = package_info
    
    return filepath, package_name


def is_so_file(filepath: str) -> bool:
    """Check if filepath is a shared object file."""
    # Match .so files but exclude certain extensions
    if any(filepath.endswith(ext) for ext in ['.so.gz', '.so.patch', '.so.diff', '.so.hmac']):
        return False
    
    return filepath.endswith('.so') or '.so.' in filepath


def extract_t64_packages(contents_file: str) -> Set[str]:
    """Extract all unique package names ending with t64."""
    print(f"Analyzing {contents_file} for t64 packages...")
    
    t64_packages = set()
    total_lines = 0
    
    with open(contents_file, 'r') as f:
        for line in f:
            total_lines += 1
            if total_lines % 100000 == 0:
                print(f"  Processed {total_lines:,} lines, found {len(t64_packages)} t64 packages...")
            
            filepath, package_name = parse_contents_line(line)
            if package_name and package_name.endswith('t64'):
                t64_packages.add(package_name)
    
    print(f"  Completed: {total_lines:,} lines processed")
    print(f"  Found: {len(t64_packages)} unique t64 packages")
    
    return t64_packages


def extract_normalized_so_names(contents_file: str) -> Dict[str, List[str]]:
    """
    Extract all normalized .so names from Contents file.
    
    Returns:
        Dict mapping normalized_name -> list of example original filenames
    """
    print(f"Analyzing {contents_file} for normalized .so names...")
    
    normalized_map = defaultdict(list)
    total_lines = 0
    so_files_found = 0
    
    with open(contents_file, 'r') as f:
        for line in f:
            total_lines += 1
            if total_lines % 100000 == 0:
                print(f"  Processed {total_lines:,} lines, found {so_files_found:,} .so files...")
            
            filepath, package_name = parse_contents_line(line)
            if not filepath:
                continue
            
            # Only process .so files
            if not is_so_file(filepath):
                continue
            
            so_files_found += 1
            
            # Get just the filename
            filename = os.path.basename(filepath)
            
            # Normalize it
            result = normalize_file_name(filename)
            
            if isinstance(result, NormalizedFileName):
                normalized_name = result.name
            else:
                # Not normalized, use as-is
                normalized_name = filename
            
            # Store example (limit to 3 examples per normalized name)
            if len(normalized_map[normalized_name]) < 3:
                normalized_map[normalized_name].append(filename)
    
    print(f"  Completed: {total_lines:,} lines processed")
    print(f"  Found: {so_files_found:,} .so files")
    print(f"  Unique normalized names: {len(normalized_map)}")
    
    return dict(normalized_map)


def compare_t64_packages(old_packages: Set[str], new_packages: Set[str],
                        old_release: str, new_release: str) -> Dict:
    """Compare t64 packages between releases."""
    return {
        'old_release': old_release,
        'new_release': new_release,
        'old_count': len(old_packages),
        'new_count': len(new_packages),
        'old_packages': sorted(old_packages),
        'new_packages': sorted(new_packages),
        'new_t64_in_new': sorted(new_packages - old_packages),
        'disappeared_t64': sorted(old_packages - new_packages)
    }


def compare_normalized_names(old_normalized: Dict[str, List[str]], 
                            new_normalized: Dict[str, List[str]],
                            old_release: str, new_release: str) -> Dict:
    """Compare normalized names between releases."""
    old_names = set(old_normalized.keys())
    new_names = set(new_normalized.keys())
    
    new_normalized_names = new_names - old_names
    removed_normalized_names = old_names - new_names
    
    return {
        'old_release': old_release,
        'new_release': new_release,
        'old_total': len(old_names),
        'new_total': len(new_names),
        'new_normalized_names': sorted(new_normalized_names),
        'removed_normalized_names': sorted(removed_normalized_names),
        'new_examples': {name: new_normalized[name] for name in sorted(new_normalized_names)},
        'removed_examples': {name: old_normalized[name] for name in sorted(removed_normalized_names)}
    }


def write_t64_report(results: Dict, output_path: Path):
    """Write t64 analysis report."""
    report_file = output_path / "t64_analysis.txt"
    print(f"\nWriting t64 analysis to {report_file}")
    
    with open(report_file, 'w') as f:
        f.write("=" * 80 + "\n")
        f.write("Ubuntu Package t64 Suffix Analysis\n")
        f.write("=" * 80 + "\n")
        f.write(f"{results['old_release']} → {results['new_release']}\n\n")
        
        f.write("SUMMARY\n")
        f.write("-" * 80 + "\n")
        f.write(f"Packages with t64 suffix in {results['old_release']}: {results['old_count']:,}\n")
        f.write(f"Packages with t64 suffix in {results['new_release']}: {results['new_count']:,}\n")
        f.write(f"Net change: {results['new_count'] - results['old_count']:+,}\n\n")
        
        # Side-by-side comparison
        f.write("SIDE-BY-SIDE COMPARISON\n")
        f.write("-" * 80 + "\n")
        f.write(f"{results['old_release']:<40} {results['new_release']:<40}\n")
        f.write(f"{'-'*40} {'-'*40}\n")
        
        # Get all unique packages
        all_packages = sorted(set(results['old_packages']) | set(results['new_packages']))
        
        for pkg in all_packages[:100]:  # Limit to first 100 for readability
            left = pkg if pkg in results['old_packages'] else "---"
            right = pkg if pkg in results['new_packages'] else "---"
            f.write(f"{left:<40} {right:<40}\n")
        
        if len(all_packages) > 100:
            f.write(f"... and {len(all_packages) - 100} more packages\n")
        f.write("\n")
        
        if results['old_count'] > 0:
            f.write(f"PACKAGES WITH t64 IN {results['old_release'].upper()} (ANOMALIES):\n")
            f.write("-" * 80 + "\n")
            f.write("NOTE: t64 packages should NOT exist before Noble (24.04)!\n")
            f.write("These are unexpected and worth investigating:\n\n")
            for i, pkg in enumerate(results['old_packages'], 1):
                f.write(f"  {i:3d}. {pkg}\n")
            f.write("\n")
        else:
            f.write(f"✓ No t64 packages found in {results['old_release']} (as expected)\n\n")
        
        if results['new_count'] > 0:
            f.write(f"PACKAGES WITH t64 IN {results['new_release'].upper()}:\n")
            f.write("-" * 80 + "\n")
            # Show first 50
            for i, pkg in enumerate(results['new_packages'][:50], 1):
                f.write(f"  {i:3d}. {pkg}\n")
            
            if len(results['new_packages']) > 50:
                f.write(f"\n  ... and {len(results['new_packages']) - 50} more\n")
            f.write("\n")
        
        if results['new_t64_in_new']:
            f.write(f"NEW t64 PACKAGES (appeared in {results['new_release']}):\n")
            f.write("-" * 80 + "\n")
            f.write(f"Count: {len(results['new_t64_in_new']):,}\n\n")
            for i, pkg in enumerate(results['new_t64_in_new'][:50], 1):
                f.write(f"  {i:3d}. {pkg}\n")
            
            if len(results['new_t64_in_new']) > 50:
                f.write(f"\n  ... and {len(results['new_t64_in_new']) - 50} more\n")
        
        f.write("\n")
        f.write("=" * 80 + "\n")
        f.write("INTERPRETATION\n")
        f.write("=" * 80 + "\n")
        f.write("The t64 suffix indicates packages using 64-bit time_t to address the\n")
        f.write("Year 2038 problem. This transition occurred in Ubuntu 24.04 (Noble).\n\n")
        f.write("Expected pattern:\n")
        f.write("  - Focal (20.04): 0 t64 packages\n")
        f.write("  - Jammy (22.04): 0 t64 packages\n")
        f.write("  - Noble (24.04): 1,500+ t64 packages\n\n")
        f.write("Any t64 packages before Noble are anomalies worth investigating.\n")


def write_normalized_names_report(results: Dict, output_path: Path):
    """Write normalized names analysis report."""
    report_file = output_path / "normalized_names_analysis.txt"
    print(f"Writing normalized names analysis to {report_file}")
    
    with open(report_file, 'w') as f:
        f.write("=" * 80 + "\n")
        f.write("Ubuntu .so File Normalized Name Changes\n")
        f.write("=" * 80 + "\n")
        f.write(f"{results['old_release']} → {results['new_release']}\n\n")
        
        f.write("SUMMARY\n")
        f.write("-" * 80 + "\n")
        f.write(f"Total normalized names in {results['old_release']}: {results['old_total']:,}\n")
        f.write(f"Total normalized names in {results['new_release']}: {results['new_total']:,}\n")
        f.write(f"Net change: {results['new_total'] - results['old_total']:+,}\n\n")
        
        f.write(f"New normalized names (appeared): {len(results['new_normalized_names']):,}\n")
        f.write(f"Removed normalized names (disappeared): {len(results['removed_normalized_names']):,}\n\n")
        
        # Side-by-side comparison of changes only
        f.write("SIDE-BY-SIDE COMPARISON (CHANGES ONLY)\n")
        f.write("-" * 160 + "\n")
        f.write(f"{results['old_release']:<80} {results['new_release']:<80}\n")
        f.write(f"{'-'*80} {'-'*80}\n")
        
        # Show all changes (removed + added)
        all_changes = sorted(set(results['removed_normalized_names']) | set(results['new_normalized_names']))
        
        for name in all_changes[:200]:  # Limit to first 200
            if name in results['removed_normalized_names']:
                left = f"- {name}"
                right = "---"
            elif name in results['new_normalized_names']:
                left = "---"
                right = f"+ {name}"
            
            f.write(f"{left:<80} {right:<80}\n")
        
        if len(all_changes) > 200:
            f.write(f"... and {len(all_changes) - 200} more changes\n")
        f.write("\n")
        
        f.write("=" * 80 + "\n")
        f.write("WHAT THIS MEANS\n")
        f.write("=" * 80 + "\n")
        f.write("Normalized names are .so filenames with version numbers removed.\n")
        f.write("Example: libssl.so.3.0.2 → normalized to 'libssl.so'\n\n")
        f.write("When a normalized name APPEARS or DISAPPEARS, it indicates:\n")
        f.write("  - A new library TYPE was introduced to the ecosystem\n")
        f.write("  - An old library TYPE was removed/deprecated\n")
        f.write("  - A library was renamed (structural change)\n\n")
        f.write("This is DIFFERENT from version changes:\n")
        f.write("  - libssl.so.1 → libssl.so.3 (version change, NOT reported here)\n")
        f.write("  - libssl.so appears/disappears (structural change, IS reported here)\n\n")
        
        if results['new_normalized_names']:
            f.write("=" * 80 + "\n")
            f.write(f"NEW NORMALIZED NAMES (appeared in {results['new_release']})\n")
            f.write("=" * 80 + "\n")
            f.write(f"Count: {len(results['new_normalized_names']):,}\n\n")
            
            for i, name in enumerate(results['new_normalized_names'][:100], 1):
                examples = results['new_examples'].get(name, [])
                examples_str = ', '.join(examples[:3])
                f.write(f"{i:3d}. {name}\n")
                f.write(f"     Examples: {examples_str}\n\n")
            
            if len(results['new_normalized_names']) > 100:
                f.write(f"... and {len(results['new_normalized_names']) - 100} more\n\n")
        else:
            f.write("No new normalized names found.\n\n")
        
        if results['removed_normalized_names']:
            f.write("=" * 80 + "\n")
            f.write(f"REMOVED NORMALIZED NAMES (disappeared from {results['new_release']})\n")
            f.write("=" * 80 + "\n")
            f.write(f"Count: {len(results['removed_normalized_names']):,}\n\n")
            
            for i, name in enumerate(results['removed_normalized_names'][:100], 1):
                examples = results['removed_examples'].get(name, [])
                examples_str = ', '.join(examples[:3])
                f.write(f"{i:3d}. {name}\n")
                f.write(f"     Last seen as: {examples_str}\n\n")
            
            if len(results['removed_normalized_names']) > 100:
                f.write(f"... and {len(results['removed_normalized_names']) - 100} more\n\n")
        else:
            f.write("No removed normalized names found.\n\n")


def write_summary_report(t64_results: Dict, normalized_results: Dict, output_path: Path):
    """Write combined summary report."""
    summary_file = output_path / "summary.txt"
    print(f"Writing summary to {summary_file}")
    
    with open(summary_file, 'w') as f:
        f.write("=" * 80 + "\n")
        f.write("Ubuntu Package Evolution Analysis Summary\n")
        f.write("=" * 80 + "\n")
        f.write(f"{t64_results['old_release']} → {t64_results['new_release']}\n")
        f.write(f"Generated by t64-normalized-analysis.py\n\n")
        
        f.write("QUESTION 1: t64 Package Suffix Analysis\n")
        f.write("-" * 80 + "\n")
        f.write(f"Packages with t64 in {t64_results['old_release']}: {t64_results['old_count']:,}\n")
        f.write(f"Packages with t64 in {t64_results['new_release']}: {t64_results['new_count']:,}\n")
        
        if t64_results['old_count'] > 0:
            f.write(f"\n⚠️  ANOMALY DETECTED: Found {t64_results['old_count']} t64 packages in {t64_results['old_release']}\n")
            f.write("   These should not exist before Noble (24.04)!\n")
        else:
            f.write(f"\n✓ As expected: No t64 packages in {t64_results['old_release']}\n")
        
        f.write("\n")
        f.write("QUESTION 2: Normalized .so Name Changes\n")
        f.write("-" * 80 + "\n")
        f.write(f"Total normalized names in {normalized_results['old_release']}: {normalized_results['old_total']:,}\n")
        f.write(f"Total normalized names in {normalized_results['new_release']}: {normalized_results['new_total']:,}\n")
        f.write(f"New library types introduced: {len(normalized_results['new_normalized_names']):,}\n")
        f.write(f"Library types removed: {len(normalized_results['removed_normalized_names']):,}\n")
        f.write(f"Net change: {normalized_results['new_total'] - normalized_results['old_total']:+,} library types\n\n")
        
        f.write("=" * 80 + "\n")
        f.write("FILES CREATED\n")
        f.write("=" * 80 + "\n")
        f.write("  summary.txt                        - This file\n")
        f.write("  t64_analysis.txt                   - Detailed t64 package analysis\n")
        f.write("  normalized_names_analysis.txt      - Detailed normalized name changes\n\n")
        
        f.write("See individual reports for detailed listings.\n")


def main():
    parser = argparse.ArgumentParser(
        description='Analyze Ubuntu Contents files for t64 packages and normalized name changes',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s ubuntu-jammy.txt ubuntu-noble.txt
  %(prog)s ubuntu-focal.txt ubuntu-jammy.txt --output-dir focal-to-jammy-analysis/
  %(prog)s ubuntu-jammy.txt ubuntu-noble.txt --output-dir jammy-to-noble-analysis/

Questions Answered:
  1. How many packages have t64 suffix? (should be 0 before Noble)
  2. What .so normalized name changes occurred? (structural changes, not version bumps)

Output Files:
  summary.txt                    - Combined summary of both analyses
  t64_analysis.txt               - Detailed t64 package listing
  normalized_names_analysis.txt  - Detailed normalized name changes
        """
    )
    
    parser.add_argument('old_file', help='Old release Contents file (e.g., ubuntu-jammy.txt)')
    parser.add_argument('new_file', help='New release Contents file (e.g., ubuntu-noble.txt)')
    parser.add_argument('--output-dir', default='t64-normalized-analysis',
                       help='Output directory (default: t64-normalized-analysis)')
    
    args = parser.parse_args()
    
    try:
        # Validate input files
        if not os.path.exists(args.old_file):
            print(f"Error: Old file not found: {args.old_file}")
            sys.exit(1)
        
        if not os.path.exists(args.new_file):
            print(f"Error: New file not found: {args.new_file}")
            sys.exit(1)
        
        # Get release names from filenames
        old_release = Path(args.old_file).stem.replace('ubuntu-', '')
        new_release = Path(args.new_file).stem.replace('ubuntu-', '')
        
        print("=" * 80)
        print("Ubuntu Package Evolution Analysis")
        print("=" * 80)
        print(f"Old release: {old_release} ({args.old_file})")
        print(f"New release: {new_release} ({args.new_file})")
        print(f"Output directory: {args.output_dir}")
        print()
        
        # Question 1: t64 packages
        print("=" * 80)
        print("QUESTION 1: Analyzing t64 Package Suffixes")
        print("=" * 80)
        old_t64 = extract_t64_packages(args.old_file)
        new_t64 = extract_t64_packages(args.new_file)
        t64_results = compare_t64_packages(old_t64, new_t64, old_release, new_release)
        print()
        
        # Question 2: normalized names
        print("=" * 80)
        print("QUESTION 2: Analyzing Normalized .so Name Changes")
        print("=" * 80)
        old_normalized = extract_normalized_so_names(args.old_file)
        new_normalized = extract_normalized_so_names(args.new_file)
        normalized_results = compare_normalized_names(old_normalized, new_normalized, old_release, new_release)
        print()
        
        # Create output directory
        output_path = Path(args.output_dir)
        output_path.mkdir(parents=True, exist_ok=True)
        
        # Write reports
        print("=" * 80)
        print("Writing Reports")
        print("=" * 80)
        write_t64_report(t64_results, output_path)
        write_normalized_names_report(normalized_results, output_path)
        write_summary_report(t64_results, normalized_results, output_path)
        
        # Final summary
        print()
        print("=" * 80)
        print("ANALYSIS COMPLETE")
        print("=" * 80)
        print(f"Results saved to: {args.output_dir}/")
        print()
        print("Quick Summary:")
        print(f"  t64 packages in {old_release}: {t64_results['old_count']:,}")
        print(f"  t64 packages in {new_release}: {t64_results['new_count']:,}")
        print(f"  New library types: {len(normalized_results['new_normalized_names']):,}")
        print(f"  Removed library types: {len(normalized_results['removed_normalized_names']):,}")
        print()
        print("See detailed reports in output directory.")
        
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()