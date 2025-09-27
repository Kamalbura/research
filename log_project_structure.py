#!/usr/bin/env python3
"""
Directory Tree and Python File Content Logger

This script creates a comprehensive log of:
1. Complete directory tree structure (like 'tree /f' command)
2. Contents of all Python (.py) files found recursively
3. Saves everything to a single .txt file

Usage:
    python log_project_structure.py [root_directory] [output_file]
    
Example:
    python log_project_structure.py . project_structure.txt
    python log_project_structure.py C:/Users/burak/Desktop/research research_complete.txt
"""

import os
import sys
import argparse
from pathlib import Path
from datetime import datetime

def log_directory_tree(root_path, output_file, skip_dirs: set | None = None):
    """Log the complete directory tree structure."""
    output_file.write("="*80 + "\n")
    output_file.write("DIRECTORY TREE STRUCTURE\n")
    output_file.write("="*80 + "\n")
    output_file.write(f"Root Directory: {root_path}\n")
    output_file.write(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
    if skip_dirs is None:
        skip_dirs = set()

    def write_tree(path, prefix="", is_last=True):
        """Recursively write tree structure."""
        try:
            items = sorted(path.iterdir())
            folders = [item for item in items if item.is_dir() and not item.name.startswith('.') and item.name not in skip_dirs]
            files = [item for item in items if item.is_file() and not item.name.startswith('.')]
            
            # Write folders first
            for i, folder in enumerate(folders):
                is_last_folder = (i == len(folders) - 1) and len(files) == 0
                connector = "└── " if is_last_folder else "├── "
                output_file.write(f"{prefix}{connector}{folder.name}/\n")
                
                extension = "    " if is_last_folder else "│   "
                write_tree(folder, prefix + extension, is_last_folder)
            
            # Write files
            for i, file in enumerate(files):
                is_last_file = (i == len(files) - 1)
                connector = "└── " if is_last_file else "├── "
                file_size = file.stat().st_size if file.exists() else 0
                output_file.write(f"{prefix}{connector}{file.name} ({file_size:,} bytes)\n")
                
        except PermissionError:
            output_file.write(f"{prefix}├── [Permission Denied]\n")
        except Exception as e:
            output_file.write(f"{prefix}├── [Error: {e}]\n")
    
    write_tree(Path(root_path))
    output_file.write("\n\n")

def log_python_files(root_path, output_file):
    """Log contents of all Python files found recursively."""
    output_file.write("="*80 + "\n")
    output_file.write("PYTHON FILE CONTENTS\n")
    output_file.write("="*80 + "\n\n")
    
    python_files = []
    
    # Find all Python files
    for root, dirs, files in os.walk(root_path):
        # Skip hidden directories
        # The caller may pass a set of directory NAMES to skip (e.g. 'tests')
        dirs[:] = [d for d in dirs if not d.startswith('.') and d != '__pycache__' and d not in SKIP_DIRS]
        
        for file in files:
            if file.endswith('.py') and not file.startswith('.'):
                python_files.append(os.path.join(root, file))
    
    python_files.sort()  # Sort for consistent output
    
    if not python_files:
        output_file.write("No Python files found.\n\n")
        return
    
    output_file.write(f"Found {len(python_files)} Python files:\n")
    for i, py_file in enumerate(python_files, 1):
        rel_path = os.path.relpath(py_file, root_path)
        output_file.write(f"  {i:2d}. {rel_path}\n")
    output_file.write("\n" + "-"*80 + "\n\n")
    
    # Log contents of each Python file
    for i, py_file in enumerate(python_files, 1):
        rel_path = os.path.relpath(py_file, root_path)
        
        output_file.write(f"FILE {i}/{len(python_files)}: {rel_path}\n")
        output_file.write("="*60 + "\n")
        output_file.write(f"Full Path: {py_file}\n")
        
        try:
            file_stat = os.stat(py_file)
            file_size = file_stat.st_size
            mod_time = datetime.fromtimestamp(file_stat.st_mtime).strftime('%Y-%m-%d %H:%M:%S')
            output_file.write(f"Size: {file_size:,} bytes\n")
            output_file.write(f"Modified: {mod_time}\n")
        except Exception as e:
            output_file.write(f"Error getting file stats: {e}\n")
        
        output_file.write("-"*60 + "\n")
        
        try:
            with open(py_file, 'r', encoding='utf-8', errors='replace') as f:
                content = f.read()
                if content.strip():
                    output_file.write(content)
                    if not content.endswith('\n'):
                        output_file.write('\n')
                else:
                    output_file.write("[Empty file]\n")
        except Exception as e:
            output_file.write(f"[Error reading file: {e}]\n")
        
        output_file.write("\n" + "="*60 + "\n\n")

def main():
    """Main function."""
    # Parse command line arguments
    parser = argparse.ArgumentParser(
        description="Log directory tree and all Python files. Optionally skip named folders (by name) e.g. 'tests,benchmarks'."
    )
    parser.add_argument("root", nargs="?", default=".", help="Root directory to analyze")
    parser.add_argument("output", nargs="?", help="Output filename (optional)")
    parser.add_argument(
        "-s",
        "--skip",
        action="append",
        help=("Folder name to skip. Can be used multiple times or as a comma-separated list. "
              "Example: -s tests -s docs or -s tests,docs"),
    )

    args = parser.parse_args()

    root_directory = args.root
    if args.output:
        output_filename = args.output
    else:
        output_filename = f"project_structure_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"

    # Build skip set (normalize to simple folder names)
    skip_dirs = set()
    if args.skip:
        for entry in args.skip:
            if not entry:
                continue
            for part in entry.split(','):
                name = part.strip()
                if not name:
                    continue
                # normalize possible paths to just the final component
                try:
                    pname = Path(name).name
                except Exception:
                    pname = name
                skip_dirs.add(pname)

    # Never allow skipping the required 'core' directory; remove it if present and warn
    if 'core' in skip_dirs:
        print("Note: 'core' is required and cannot be skipped; ignoring 'core' in --skip list.")
        skip_dirs.discard('core')

    # Make skip set available to module-level walker via global used below
    global SKIP_DIRS
    SKIP_DIRS = skip_dirs

    if SKIP_DIRS:
        print(f"Skipping directories by name: {', '.join(sorted(SKIP_DIRS))}")
    
    # Resolve paths
    root_path = Path(root_directory).resolve()
    output_path = Path(output_filename).resolve()
    
    if not root_path.exists():
        print(f"Error: Root directory '{root_path}' does not exist!")
        sys.exit(1)
    
    if not root_path.is_dir():
        print(f"Error: '{root_path}' is not a directory!")
        sys.exit(1)
    
    print(f"Analyzing directory: {root_path}")
    print(f"Output file: {output_path}")
    print("Processing...")
    
    try:
        with open(output_path, 'w', encoding='utf-8') as output_file:
            # Write header
            output_file.write("PROJECT STRUCTURE AND PYTHON FILES LOG\n")
            output_file.write("="*80 + "\n")
            output_file.write(f"Root Directory: {root_path}\n")
            output_file.write(f"Output File: {output_path}\n")
            output_file.write(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            output_file.write("="*80 + "\n\n")
            
            # Log directory tree
            log_directory_tree(root_path, output_file)
            
            # Log Python file contents
            log_python_files(root_path, output_file)
            
            # Write footer
            output_file.write("="*80 + "\n")
            output_file.write("END OF LOG\n")
            output_file.write("="*80 + "\n")
    
    except Exception as e:
        print(f"Error writing to output file: {e}")
        sys.exit(1)
    
    print(f"✅ Successfully created: {output_path}")
    print(f"📁 Log contains directory tree + all Python file contents")

if __name__ == "__main__":
    main()