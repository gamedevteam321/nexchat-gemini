#!/usr/bin/env python3

# Comprehensive fix for indentation errors in api.py
import re

def fix_file():
    with open('api.py', 'r') as f:
        content = f.read()
    
    # Fix the try/except block around line 1800-1803
    # Look for the pattern where try is followed by except with wrong indentation
    content = re.sub(
        r'(\s+try:\n\s+frappe\.log_error\([^)]+\), "Early Detection"\)\n)(\s+except:)',
        r'\1                    except:',
        content
    )
    
    # Fix multiple other indentation issues
    lines = content.split('\n')
    fixed_lines = []
    
    for i, line in enumerate(lines):
        # Fix line 1728: matching_options
        if i == 1727 and 'matching_options = [opt for opt in numbered_options' in line:
            fixed_lines.append('                    matching_options = [opt for opt in numbered_options if user_input.lower() in opt.lower()]')
        
        # Fix line 2071: except over-indented
        elif i == 2070 and '                    except:' in line:
            fixed_lines.append('            except:')
        
        # Fix line 2072: pass over-indented
        elif i == 2071 and '                        pass' in line:
            fixed_lines.append('                pass')
        
        # Fix line 2082: else misaligned
        elif i == 2081 and line.strip() == 'else:' and not line.startswith('            else:'):
            fixed_lines.append('            else:')
        
        # Fix line 2703: missing indent
        elif i == 2702 and 'missing_fields.append(field)' in line and not line.startswith('                    '):
            fixed_lines.append('                    missing_fields.append(field)')
        
        # Fix line 2753: missing indent  
        elif i == 2752 and 'field_to_ask = missing_fields[0]' in line and not line.startswith('            '):
            fixed_lines.append('            field_to_ask = missing_fields[0]')
        
        # Fix line 2883: else misaligned
        elif i == 2882 and line.strip() == 'else:' and '        else:' not in line:
            fixed_lines.append('                        else:')
        
        # Fix line 4940: missing indent
        elif i == 4939 and 'selected_value = numbered_options[num - 1]' in line and not line.startswith('                    '):
            fixed_lines.append('                    selected_value = numbered_options[num - 1]')
        
        # Fix line 4944: missing indent  
        elif i == 4943 and 'return f"❌ Invalid input. Please use numbers or direct input."' in line and not line.startswith('                '):
            fixed_lines.append('                return f"❌ Invalid input. Please use numbers or direct input."')
        
        # Fix line 4972: else misaligned
        elif i == 4971 and line.strip() == 'else:' and 'else:' in line and not line.startswith('            '):
            fixed_lines.append('            else:')
        
        else:
            fixed_lines.append(line)
    
    # Join lines back together
    content = '\n'.join(fixed_lines)
    
    with open('api.py', 'w') as f:
        f.write(content)
    
    print("Applied comprehensive fixes")

if __name__ == "__main__":
    fix_file()
