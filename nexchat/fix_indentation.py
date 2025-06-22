#!/usr/bin/env python3

# Fix specific indentation errors in api.py
import re

def fix_indentation():
    with open('api.py', 'r') as f:
        lines = f.readlines()
    
    # Fix line 1728: matching_options should be indented properly
    if len(lines) > 1727:
        line = lines[1727]  # 0-based indexing
        if 'matching_options = [opt for opt in numbered_options' in line:
            lines[1727] = '                    matching_options = [opt for opt in numbered_options if user_input.lower() in opt.lower()]\n'
    
    # Fix line 2071: except should not be over-indented
    if len(lines) > 2070:
        line = lines[2070]  # 0-based indexing  
        if '                    except:' in line:
            lines[2070] = '            except:\n'
    
    # Fix line 2072: pass should be properly indented
    if len(lines) > 2071:
        line = lines[2071]  # 0-based indexing
        if '                        pass' in line:
            lines[2071] = '                pass\n'
    
    # Fix line 2074: if should not be expected expression
    if len(lines) > 2073:
        line = lines[2073]  # 0-based indexing
        if line.strip().startswith('if missing_child_tables:'):
            # Make sure it's properly indented
            lines[2073] = '            if missing_child_tables:\n'
    
    # Fix line 2076: child_table_to_collect should be properly indented  
    if len(lines) > 2075:
        line = lines[2075]  # 0-based indexing
        if 'child_table_to_collect = missing_child_tables[0]' in line:
            lines[2075] = '                child_table_to_collect = missing_child_tables[0]\n'
    
    # Fix line 2082: else should be properly aligned
    if len(lines) > 2081:
        line = lines[2081]  # 0-based indexing
        if line.strip().startswith('else:'):
            lines[2081] = '            else:\n'
    
    # Fix other similar issues that might exist
    for i, line in enumerate(lines):
        # Look for lines that have too much indentation in except blocks
        if line.strip().startswith('except:') and line.startswith('                    except:'):
            lines[i] = '            except:\n'
        # Look for lines that have too much indentation in pass statements
        elif line.strip() == 'pass' and line.startswith('                        pass'):
            lines[i] = '                pass\n'
    
    with open('api.py', 'w') as f:
        f.writelines(lines)
    
    print("Fixed indentation errors")

if __name__ == "__main__":
    fix_indentation()
