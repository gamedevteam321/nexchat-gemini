import frappe
import json
import traceback
from frappe import _

try:
    import google.generativeai as genai
except ImportError:
    genai = None

# --- Conversation State Management ---
def get_conversation_state(user):
    """Get the current conversation state for a user"""
    return frappe.cache().get_value(f"nexchat_state_{user}")

def set_conversation_state(user, state):
    """Set conversation state for a user (expires in 10 minutes)"""
    frappe.cache().set_value(f"nexchat_state_{user}", state, expires_in_sec=600)

def clear_conversation_state(user):
    """Clear conversation state for a user"""
    frappe.cache().delete_value(f"nexchat_state_{user}")

def is_new_action_request(message):
    """Check if the user is trying to start a new action instead of continuing the current conversation"""
    message_lower = message.lower().strip()
    
    # Keywords that indicate a new action request
    new_action_keywords = [
        # Action verbs
        'create', 'make', 'add', 'new',
        'list', 'show', 'display', 'get', 'find', 'search',
        'update', 'change', 'modify', 'edit', 'set',
        'delete', 'remove', 'cancel',
        'assign', 'give',
        
        # Information requests
        'show all', 'list all', 'display all', 'all roles', 'all users', 'all customers',
        
        # Navigation/control
        'help', 'back', 'main menu', 'start over', 'restart',
        'cancel', 'nevermind', 'stop', 'quit', 'exit'
    ]
    
    # Check if message starts with any of these keywords
    for keyword in new_action_keywords:
        if message_lower.startswith(keyword) or f' {keyword}' in f' {message_lower}':
            return True
    
    # Check for "show all X" pattern specifically
    if message_lower.startswith('show all') or message_lower.startswith('list all'):
        return True
        
    return False

# --- Main API Endpoint ---
@frappe.whitelist()
def process_message(message):
    """Main function called from the frontend to process user messages"""
    try:
        user = frappe.session.user
        state = get_conversation_state(user) or {}

        # Check if user wants to cancel or start a new action during conversation
        if state and is_new_action_request(message):
            clear_conversation_state(user)
            # Process as new request
            json_response = get_intent_from_gemini(message, user)
            response = execute_task(json_response, user)
        # If we are in the middle of collecting information for a task
        elif state.get("action") == "collect_fields":
            response = handle_field_collection(message, state, user)
        elif state.get("action") == "collect_role":
            response = handle_role_collection(message, state, user)
        elif state.get("action") == "collect_role_selection":
            response = handle_role_selection_collection(message, state, user)
        elif state.get("action") == "collect_stock_selection":
            # Debug for Purchase Order specifically
            try:
                if state.get("doctype") == "Purchase Order":
                    frappe.log_error(f"PO state: {state.get('selection_type')}, msg: {message}", "PO Debug")
            except:
                pass
            response = handle_stock_selection_collection(message, state, user)
        elif state.get("action") == "collect_item_details":
            response = handle_item_details_collection(message, state, user)
        elif state.get("action") == "collect_transaction_item_details":
            response = handle_transaction_item_details_collection(message, state, user)
        elif state.get("action") == "collect_update_info":
            response = handle_update_info_collection(message, state, user)
        elif state.get("action") == "collect_update_value":
            response = handle_update_value_collection(message, state, user)
        else:
            # This is a new request, send it to Gemini for intent recognition
            json_response = get_intent_from_gemini(message, user)
            
            # Execute the task based on Gemini's understanding
            response = execute_task(json_response, user)

        return {"response": response}
    
    except Exception as e:
        # Enhanced error logging for debugging
        import traceback
        error_msg = str(e)[:200] + "..." if len(str(e)) > 200 else str(e)
        full_error = f"Nexchat Error: {error_msg}\nUser: {user}\nMessage: {message}\nTraceback: {traceback.format_exc()}"
        frappe.log_error(full_error, "Nexchat Processing Error")
        return {"response": f"Sorry, I encountered an error processing your request. Please try again. (Error: {str(e)[:100]})"}

def handle_field_collection(message, state, user):
    """Handle collection of required fields for document creation"""
    try:
        # Add the new piece of information
        field_to_collect = state["missing_fields"][0]
        state["data"][field_to_collect] = message.strip()

        # Remove the collected field from the list of missing fields
        state["missing_fields"].pop(0)

        # Check if we still have missing fields
        if state["missing_fields"]:
            # Still have more fields to collect
            field_to_ask = state["missing_fields"][0]
            doctype = state["doctype"]
            
            # Special handling for Stock Entry fields with interactive selection
            if doctype == "Stock Entry":
                data = state["data"]
                # Always prioritize critical fields in order and check for items
                has_stock_type = data.get("stock_entry_type") or data.get("purpose")
                has_company = data.get("company")
                has_source_warehouse = data.get("from_warehouse") or data.get("s_warehouse")
                has_items = data.get("items_list") and len(data.get("items_list", [])) > 0
                
                if not has_stock_type:
                    return show_stock_entry_type_selection(state["data"], state["missing_fields"], user)
                elif not has_company:
                    return show_company_selection(state["data"], state["missing_fields"], user, "Stock Entry")
                elif not has_source_warehouse:
                    # Need source warehouse for most stock entry types
                    return show_warehouse_selection("from_warehouse", state["data"], state["missing_fields"], user)
                elif not has_items:
                    # Always need items for Stock Entry
                    return show_items_selection(state["data"], state["missing_fields"], user)
                elif field_to_ask in ["from_warehouse", "to_warehouse", "s_warehouse", "t_warehouse"]:
                    return show_warehouse_selection(field_to_ask, state["data"], state["missing_fields"], user)
                else:
                    # Debug: Log what field is being asked for
                    frappe.log_error(f"Stock Entry field being asked: {field_to_ask}", "Nexchat Debug")
            
            # Special handling for Asset fields with interactive selection
            elif doctype == "Asset":
                if field_to_ask == "company":
                    return show_company_selection(state["data"], state["missing_fields"], user, "Asset")
                elif field_to_ask == "item_code":
                    return show_asset_item_selection(state["data"], state["missing_fields"], user)
                elif field_to_ask == "location":
                    return show_location_selection(state["data"], state["missing_fields"], user)
                elif field_to_ask in ["asset_category", "asset_owner"]:
                    return show_asset_field_selection(field_to_ask, state["data"], state["missing_fields"], user)
            
            # Default field collection
            meta = frappe.get_meta(doctype)
            field_obj = meta.get_field(field_to_ask)
            label_to_ask = field_obj.label or field_to_ask
            
            # Update conversation state with the new data
            set_conversation_state(user, state)
            
            return f"Great! Now, what should I set as the {label_to_ask}?"
        else:
            # All fields collected
            doctype = state["doctype"]
            data = state["data"]
            
            # Special check for Stock Entry to ensure we have items
            if doctype == "Stock Entry":
                has_items = data.get("items_list") and len(data.get("items_list", [])) > 0
                if not has_items:
                    # Need to collect items
                    return show_items_selection(data, [], user)
            
            # Clear conversation state since we're done collecting
            clear_conversation_state(user)
            
            # Create the document
            return create_document(doctype, data, user)
    
    except Exception as e:
        clear_conversation_state(user)
        return f"Error processing your input: {str(e)}"

def handle_role_collection(message, state, user):
    """Handle collection of role name for assignment (legacy handler)"""
    try:
        role_name = message.strip()
        target_user = state.get("target_user")
        available_roles = state.get("available_roles", [])
        
        # Clear conversation state
        clear_conversation_state(user)
        
        # Check if the provided role is valid
        if role_name not in available_roles:
            # Try to find a close match
            matching_roles = [role for role in available_roles if role_name.lower() in role.lower()]
            if matching_roles:
                if len(matching_roles) == 1:
                    role_name = matching_roles[0]
                else:
                    role_list = "\n".join([f"• {role}" for role in matching_roles])
                    return f"I found multiple roles matching '{role_name}':\n{role_list}\n\nPlease specify the exact role name."
            else:
                return f"Role '{role_name}' not found. Please check the role name and try again."
        
        # Assign the role
        return assign_role_to_user(target_user, role_name)
        
    except Exception as e:
        clear_conversation_state(user)
        return f"Error processing role assignment: {str(e)}"

def handle_role_selection_collection(message, state, user):
    """Handle collection of role selection with numbers and multiple selection"""
    try:
        target_user = state.get("target_user")
        available_roles = state.get("available_roles", [])
        numbered_roles = state.get("numbered_roles", [])
        user_input = message.strip()
        
        # Handle special commands
        if user_input.lower() in ['cancel', 'quit', 'exit']:
            clear_conversation_state(user)
            return "Role assignment cancelled."
        
        if user_input.lower() in ['all roles', 'assign all', 'assign all roles', '*', 'all *']:
            # Assign all available roles
            clear_conversation_state(user)
            return assign_all_roles_to_user(target_user, available_roles)
        
        if user_input.lower() == 'all':
            # Show detailed role list
            clear_conversation_state(user)
            return handle_list_roles_request()
        
        # Parse the input for role selection
        selected_roles = []
        
        # Check if input contains numbers (comma-separated or single)
        if any(char.isdigit() for char in user_input):
            # Parse numbers
            numbers = []
            for part in user_input.replace(' ', '').split(','):
                try:
                    num = int(part.strip())
                    if 1 <= num <= len(numbered_roles):
                        numbers.append(num - 1)  # Convert to 0-based index
                    else:
                        return f"❌ Invalid number: {num}. Please use numbers between 1 and {len(numbered_roles)}."
                except ValueError:
                    return f"❌ Invalid input: '{part}'. Please use numbers separated by commas (e.g., 1,3,5) or role names."
            
            # Get selected roles by numbers
            selected_roles = [numbered_roles[i] for i in numbers]
        
        else:
            # Try to match role name directly
            role_name = user_input
            matching_roles = [role for role in available_roles if role_name.lower() in role.lower()]
            
            if not matching_roles:
                return f"❌ Role '{role_name}' not found. Please use numbers (e.g., 1,3,5) or exact role names."
            elif len(matching_roles) == 1:
                selected_roles = matching_roles
            else:
                role_list = "\n".join([f"• {role}" for role in matching_roles])
                return f"Multiple roles found matching '{role_name}':\n{role_list}\n\nPlease be more specific or use numbers."
        
        # Clear conversation state before assignment
        clear_conversation_state(user)
        
        # Assign the selected roles
        if len(selected_roles) == 1:
            return assign_role_to_user(target_user, selected_roles[0])
        else:
            return assign_multiple_roles_to_user(target_user, selected_roles)
        
    except Exception as e:
        clear_conversation_state(user)
        return f"Error processing role selection: {str(e)}"

def handle_update_info_collection(message, state, user):
    """Handle collection of field and value for update"""
    try:
        doctype = state.get("doctype")
        filters = state.get("filters")
        doc_name = state.get("doc_name")
        
        # Parse the user input to extract field and value
        # Try to match patterns like "update field_name to value" or "set field_name to value"
        import re
        
        # Pattern matching for update syntax
        patterns = [
            r'(?:update|set)\s+(\w+)\s+to\s+(.+)',
            r'(\w+)\s+to\s+(.+)',
            r'(\w+)\s*=\s*(.+)',
            r'(\w+)\s+(.+)'
        ]
        
        field_name = None
        new_value = None
        
        for pattern in patterns:
            match = re.search(pattern, message.strip(), re.IGNORECASE)
            if match:
                field_name = match.group(1).strip()
                new_value = match.group(2).strip()
                break
        
        if not field_name or not new_value:
            # If we can't parse, ask for clarification
            return "Please specify the field and value like: 'Update field_name to new_value' or 'field_name to new_value'"
        
        # Clear conversation state
        clear_conversation_state(user)
        
        # Create update task and execute
        update_task = {
            "doctype": doctype,
            "action": "update",
            "filters": filters,
            "data": {field_name: new_value}
        }
        
        return handle_update_action(doctype, update_task, user)
        
    except Exception as e:
        clear_conversation_state(user)
        return f"Error processing update: {str(e)}"

def handle_update_value_collection(message, state, user):
    """Handle collection of new value for a specific field"""
    try:
        doctype = state.get("doctype")
        filters = state.get("filters")
        field_to_update = state.get("field_to_update")
        new_value = message.strip()
        
        # Clear conversation state
        clear_conversation_state(user)
        
        # Create update task and execute
        update_task = {
            "doctype": doctype,
            "action": "update",
            "filters": filters,
            "data": {field_to_update: new_value}
        }
        
        return handle_update_action(doctype, update_task, user)
        
    except Exception as e:
        clear_conversation_state(user)
        return f"Error processing update: {str(e)}"

def handle_stock_selection_collection(message, state, user):
    """Handle collection of stock entry field selections"""
    try:
        selection_type = state.get("selection_type")
        data = state.get("data")
        missing_fields = state.get("missing_fields")
        numbered_options = state.get("numbered_options", [])
        user_input = message.strip()
        
        # Handle cancel
        if user_input.lower() in ['cancel', 'quit', 'exit']:
            clear_conversation_state(user)
            doctype_name = state.get("doctype", "Document")
            return f"{doctype_name} creation cancelled."
        
        selected_value = None
        
        # Special handling for different field types
        if selection_type in ["gross_purchase_amount"] or state.get("field_type") == "Currency":
            try:
                # Convert to float for amount validation
                amount = float(user_input)
                if amount < 0:
                    return "❌ Amount cannot be negative. Please enter a valid amount."
                selected_value = amount
            except ValueError:
                return "❌ Invalid amount. Please enter a number (e.g., 50000, 25000.50) or type 'cancel' to cancel."
        
        # Handle date input validation
        elif state.get("field_type") == "Date":
            import re
            from datetime import datetime
            
            # Check if it's a numbered option first
            if user_input.isdigit() and numbered_options:
                try:
                    num = int(user_input)
                    if 1 <= num <= len(numbered_options):
                        selected_value = numbered_options[num - 1]
                    else:
                        return f"❌ Invalid number: {num}. Please use numbers between 1 and {len(numbered_options)}."
                except ValueError:
                    return "❌ Invalid input. Please use numbers or date format."
            else:
                # Validate date format (YYYY-MM-DD)
                if re.match(r'^\d{4}-\d{2}-\d{2}$', user_input):
                    try:
                        # Validate the date
                        datetime.strptime(user_input, '%Y-%m-%d')
                        selected_value = user_input
                    except ValueError:
                        return "❌ Invalid date. Please use YYYY-MM-DD format (e.g., 2024-12-25)."
                else:
                    return "❌ Invalid date format. Please use YYYY-MM-DD format (e.g., 2024-12-25) or select a numbered option."
        
        # Handle numeric input validation
        elif state.get("field_type") in ["Int", "Float", "Percent"]:
            try:
                if state.get("field_type") == "Int":
                    selected_value = int(float(user_input))  # Allow decimal input but convert to int
                else:
                    selected_value = float(user_input)
            except ValueError:
                return f"❌ Invalid number. Please enter a valid {state.get('field_type', 'number').lower()}."
        
        # Check if input is a number (for numbered options)
        elif user_input.isdigit() and numbered_options:
            try:
                num = int(user_input)
                if 1 <= num <= len(numbered_options):
                    selected_value = numbered_options[num - 1]
                else:
                    return f"❌ Invalid number: {num}. Please use numbers between 1 and {len(numbered_options)}."
            except ValueError:
                return f"❌ Invalid input. Please use numbers (e.g., 1, 2, 3) or type the option name."
        else:
            # Try to match the text directly (for non-numeric options)
            if numbered_options:
                matching_options = [opt for opt in numbered_options if user_input.lower() in opt.lower()]
                if len(matching_options) == 1:
                    selected_value = matching_options[0]
                elif len(matching_options) > 1:
                    return f"Multiple options found matching '{user_input}'. Please be more specific or use numbers."
                else:
                    return f"Option '{user_input}' not found. Please use numbers (e.g., 1, 2, 3) or exact option names."
            else:
                # If no numbered options, treat as direct input
                selected_value = user_input
        
        # Add the selected value to data
        selection_type = state.get("selection_type")
        if selection_type:
            # Handle special cases for location creation
            if selection_type == "location":
                # Check if location exists, create if it doesn't
                if not frappe.db.exists("Location", selected_value):
                    try:
                        new_location = frappe.new_doc("Location")
                        new_location.location_name = selected_value
                        new_location.insert()
                        frappe.db.commit()
                    except Exception as e:
                        return f"Could not create location '{selected_value}': {str(e)}. Please use an existing location."
            
            data[selection_type] = selected_value
            
            # CRITICAL FIX: Detect doctype immediately after naming_series selection
            if selection_type == "naming_series":
                detected_doctype = None
                if "ACC-PINV-" in selected_value:
                    detected_doctype = "Purchase Invoice"
                elif "PUR-ORD-" in selected_value:
                    detected_doctype = "Purchase Order"
                elif "ACC-SINV-" in selected_value:
                    detected_doctype = "Sales Invoice"
                elif "SO-" in selected_value:
                    detected_doctype = "Sales Order"
                
                if detected_doctype:
                    state["doctype"] = detected_doctype
                    set_conversation_state(user, state)  # CRITICAL: Save the updated state immediately
                    try:
                        frappe.log_error(f"EARLY doctype detection from series: {detected_doctype} (series: {selected_value})", "Early Detection")
                    except:
                        pass
            
            # Also map to common warehouse field names for compatibility
            if selection_type == "from_warehouse":
                data["s_warehouse"] = selected_value
            elif selection_type == "to_warehouse":
                data["t_warehouse"] = selected_value
        else:
            current_field = missing_fields[0]
            data[current_field] = selected_value
        
        # Remove this field from missing fields - handle special cases
        remaining_fields = missing_fields.copy()
        
        # Remove the field we just populated
        if selection_type and selection_type in remaining_fields:
            remaining_fields.remove(selection_type)
        elif missing_fields:
            remaining_fields.pop(0)
        
        # For warehouse selections, also remove related field names
        if selection_type == "from_warehouse" and "s_warehouse" in remaining_fields:
            remaining_fields.remove("s_warehouse")
        elif selection_type == "to_warehouse" and "t_warehouse" in remaining_fields:
            remaining_fields.remove("t_warehouse")
        
        # Get current doctype from state - CRITICAL: This must be preserved!
        current_doctype = state.get("doctype")
        
        # Debug: Log the doctype from state and data
        try:
            frappe.log_error(f"Doctype from state: {current_doctype}, Data: {data}, Selection: {selection_type}", "State Debug")
        except:
            pass
        
        # CRITICAL FIX: If doctype is not set, detect from naming series
        if not current_doctype and data.get("naming_series"):
            series = data.get("naming_series", "")
            if "ACC-PINV-" in series:
                current_doctype = "Purchase Invoice"
            elif "PUR-ORD-" in series:
                current_doctype = "Purchase Order"
            elif "ACC-SINV-" in series:
                current_doctype = "Sales Invoice"
            elif "SO-" in series:
                current_doctype = "Sales Order"
            
            if current_doctype:
                # Update state with correct doctype
                state["doctype"] = current_doctype
                set_conversation_state(user, state)
                try:
                    frappe.log_error(f"Detected doctype from series: {current_doctype} ({series})", "Doctype Detection")
                except:
                    pass
        
        # For Stock Entry ONLY, check if we need to collect items
        if current_doctype == "Stock Entry":
            has_stock_type = data.get("stock_entry_type") or data.get("purpose")
            has_company = data.get("company")
            has_source_warehouse = data.get("from_warehouse") or data.get("s_warehouse")
            has_target_warehouse = data.get("to_warehouse") or data.get("t_warehouse")
            has_items = data.get("items_list") and len(data.get("items_list", [])) > 0
            
            # Check warehouse requirements based on stock entry type
            stock_entry_type = has_stock_type
            warehouse_requirement_met = False
            
            if stock_entry_type == "Material Receipt":
                # Material Receipt only needs target warehouse
                warehouse_requirement_met = has_target_warehouse
                if has_stock_type and has_company and not has_target_warehouse:
                    # Need to collect target warehouse first
                    return show_warehouse_selection("to_warehouse", data, remaining_fields, user)
            elif stock_entry_type == "Material Issue":
                # Material Issue only needs source warehouse
                warehouse_requirement_met = has_source_warehouse
                if has_stock_type and has_company and not has_source_warehouse:
                    # Need to collect source warehouse first
                    return show_warehouse_selection("from_warehouse", data, remaining_fields, user)
            elif stock_entry_type in ["Material Transfer", "Material Transfer for Manufacture"]:
                # These need both warehouses
                warehouse_requirement_met = has_source_warehouse and has_target_warehouse
                if has_stock_type and has_company:
                    if not has_source_warehouse:
                        return show_warehouse_selection("from_warehouse", data, remaining_fields, user)
                    elif not has_target_warehouse:
                        return show_warehouse_selection("to_warehouse", data, remaining_fields, user)
            else:
                # For other types like Manufacture, Repack - may need source warehouse
                warehouse_requirement_met = has_source_warehouse
                if has_stock_type and has_company and not has_source_warehouse:
                    return show_warehouse_selection("from_warehouse", data, remaining_fields, user)
            
            if has_stock_type and has_company and warehouse_requirement_met and not has_items:
                # Ready to collect items for Stock Entry
                return show_items_selection(data, remaining_fields, user)
        elif remaining_fields:
            # Continue with next field
            next_field = remaining_fields[0]
            
            # Handle Stock Entry specific fields first
            if next_field in ["stock_entry_type", "purpose", "voucher_type"]:
                return show_stock_entry_type_selection(data, remaining_fields, user)
            elif next_field in ["from_warehouse", "to_warehouse", "s_warehouse", "t_warehouse"]:
                return show_warehouse_selection(next_field, data, remaining_fields, user)
            elif next_field == "items":
                return show_items_selection(data, remaining_fields, user)
            else:
                # For Purchase Order, check if we have basic fields and can proceed to items
                if current_doctype == "Purchase Order":
                    has_series = data.get("naming_series")
                    has_supplier = data.get("supplier") 
                    has_company = data.get("company")
                    has_date = data.get("transaction_date")
                    
                    # If we have all basic required fields, go to items collection
                    if has_series and has_supplier and has_company:
                        # Set default date if missing
                        if not has_date:
                            from datetime import date
                            data["transaction_date"] = date.today().strftime("%Y-%m-%d")
                        
                        # Ready to collect items for Purchase Order
                        return show_transaction_items_selection(current_doctype, data, remaining_fields, user)
                
                # For Purchase Invoice, check if we have basic fields and can proceed to items  
                elif current_doctype == "Purchase Invoice":
                    has_series = data.get("naming_series")
                    has_supplier = data.get("supplier")
                    has_company = data.get("company") 
                    has_date = data.get("posting_date")
                    
                    # If we have all basic required fields, go to items collection
                    if has_series and has_supplier and has_company:
                        # Set default date if missing
                        if not has_date:
                            from datetime import date
                            data["posting_date"] = date.today().strftime("%Y-%m-%d")
                        
                        # Ready to collect items for Purchase Invoice
                        return show_transaction_items_selection(current_doctype, data, remaining_fields, user)
                
                # Use smart field selection for all other fields
                # CRITICAL: Don't default to Stock Entry, use current doctype from state
                try:
                    frappe.log_error(f"Before detection: {current_doctype}, {next_field}", "Debug Flow")
                except:
                    pass
                
                if not current_doctype:
                    # CRITICAL: Check naming series FIRST before other detection
                    if data.get("naming_series"):
                        series = data.get("naming_series", "")
                        if "ACC-PINV-" in series:
                            current_doctype = "Purchase Invoice"
                        elif "PUR-ORD-" in series:
                            current_doctype = "Purchase Order"
                        elif "ACC-SINV-" in series:
                            current_doctype = "Sales Invoice"
                        elif "SO-" in series:
                            current_doctype = "Sales Order"
                    
                    # Only detect if not already set from naming series
                    if not current_doctype and "supplier" in data:
                        # Could be Purchase Order or Purchase Invoice
                        if any(field in data for field in ["bill_no", "bill_date", "due_date"]):
                            current_doctype = "Purchase Invoice"
                        else:
                            current_doctype = "Purchase Order"
                    elif "customer" in data:
                        # Could be Sales Order, Sales Invoice, or Quotation
                        if any(field in data for field in ["due_date", "posting_date"]) and "delivery_date" not in data:
                            current_doctype = "Sales Invoice"
                        elif "valid_till" in data:
                            current_doctype = "Quotation"
                        else:
                            current_doctype = "Sales Order"
                    elif "employee_name" in data or "first_name" in data:
                        current_doctype = "Employee"
                    elif "customer_name" in data:
                        current_doctype = "Customer"
                    elif "supplier_name" in data:
                        current_doctype = "Supplier"
                    elif "item_name" in data and not any(field in data for field in ["location", "gross_purchase_amount"]):
                        current_doctype = "Item"
                    elif "item_code" in data and any(field in data for field in ["location", "gross_purchase_amount"]):
                        current_doctype = "Asset"
                    elif "stock_entry_type" in data or "purpose" in data or any(field in data for field in ["from_warehouse", "to_warehouse", "s_warehouse", "t_warehouse"]):
                        current_doctype = "Stock Entry"
                    else:
                        # Final fallback
                        current_doctype = "Stock Entry"
                
                try:
                    frappe.log_error(f"Getting meta: {current_doctype}, {next_field}", "Debug Meta")
                except:
                    pass
                
                meta = frappe.get_meta(current_doctype)
                field_obj = meta.get_field(next_field)
                
                try:
                    frappe.log_error(f"Field obj: {field_obj is not None}", "Debug Field")
                except:
                    pass
                
                if field_obj:
                    # Use smart field selection
                    return get_smart_field_selection(next_field, field_obj, data, remaining_fields, user, current_doctype)
                else:
                    # Fallback for fields not found in metadata
                    label_to_ask = next_field.replace("_", " ").title()
                    
                    state = {
                        "action": "collect_fields",
                        "doctype": current_doctype,
                        "data": data,
                        "missing_fields": remaining_fields
                    }
                    set_conversation_state(user, state)
                    
                    return f"Great! Now, what should I set as the {label_to_ask}?"
        else:
            # All fields collected - use current doctype already retrieved above
            # CRITICAL FIX: Only run detection logic if doctype is not set in state
            # If we already have a doctype from state, NEVER override it!
            if not current_doctype:
                # CRITICAL: Check naming series FIRST before other detection
                if data.get("naming_series"):
                    series = data.get("naming_series", "")
                    if "ACC-PINV-" in series:
                        current_doctype = "Purchase Invoice"
                    elif "PUR-ORD-" in series:
                        current_doctype = "Purchase Order"
                    elif "ACC-SINV-" in series:
                        current_doctype = "Sales Invoice"
                    elif "SO-" in series:
                        current_doctype = "Sales Order"
                
                # Only use field-based detection if naming series didn't work
                if not current_doctype:
                    # Comprehensive doctype detection based on field patterns
                    if "supplier" in data:
                        # Could be Purchase Order or Purchase Invoice
                        if any(field in data for field in ["bill_no", "bill_date", "due_date"]):
                            current_doctype = "Purchase Invoice"
                        else:
                            current_doctype = "Purchase Order"
                elif "customer" in data:
                    # Could be Sales Order, Sales Invoice, or Quotation
                    if any(field in data for field in ["due_date", "posting_date"]) and "delivery_date" not in data:
                        current_doctype = "Sales Invoice"
                    elif "valid_till" in data:
                        current_doctype = "Quotation"
                    else:
                        current_doctype = "Sales Order"
                elif "employee_name" in data or "first_name" in data:
                    current_doctype = "Employee"
                elif "customer_name" in data:
                    current_doctype = "Customer"
                elif "supplier_name" in data:
                    current_doctype = "Supplier"
                elif "item_name" in data and not any(field in data for field in ["location", "gross_purchase_amount"]):
                    current_doctype = "Item"
                elif "item_code" in data and any(field in data for field in ["location", "gross_purchase_amount"]):
                    current_doctype = "Asset"
                elif "stock_entry_type" in data or "purpose" in data or any(field in data for field in ["from_warehouse", "to_warehouse", "s_warehouse", "t_warehouse"]):
                    current_doctype = "Stock Entry"
                else:
                    # Final fallback
                    current_doctype = "Stock Entry"
            
            # ULTIMATE FAILSAFE: Force correct doctype detection if it's still wrong
            if current_doctype == "Stock Entry" and data.get("naming_series"):
                series = data.get("naming_series", "")
                if "ACC-PINV-" in series:
                    current_doctype = "Purchase Invoice"
                    try:
                        frappe.log_error(f"FAILSAFE: Corrected Stock Entry to Purchase Invoice", "Failsafe Correction")
                    except:
                        pass
                elif "PUR-ORD-" in series:
                    current_doctype = "Purchase Order"
            
            # Debug: Log the final doctype determination
            try:
                frappe.log_error(f"Final doctype: {current_doctype}, Data keys: {list(data.keys())}", "Doctype Debug")
            except:
                pass
            
            # Handle doctype-specific logic based on FINAL determined doctype
            if current_doctype == "Stock Entry":
                # For Stock Entry, check if we have items
                has_items = data.get("items_list") and len(data.get("items_list", [])) > 0
                if has_items:
                    # All good, create the document
                    clear_conversation_state(user)
                    return create_document("Stock Entry", data, user)
                else:
                    # Missing items, go to Stock Entry item selection
                    return show_items_selection(data, [], user)
            elif current_doctype in ["Purchase Order", "Sales Order", "Purchase Invoice", "Sales Invoice", "Quotation"]:
                # For transaction documents, check if they need items
                has_transaction_items = data.get("items") or data.get("items_list")
                
                if not has_transaction_items:
                    # CRITICAL FIX: Need to collect items for transaction documents
                    try:
                        frappe.log_error(f"Going to transaction items for: {current_doctype}", "Transaction Items Debug")
                    except:
                        pass
                    return show_transaction_items_selection(current_doctype, data, [], user)
                else:
                    # All good, create the document
                    clear_conversation_state(user)
                    return create_document(current_doctype, data, user)
            else:
                # For other doctypes (Asset, Customer, Supplier, etc.), create directly
                clear_conversation_state(user)
                return create_document(current_doctype, data, user)
            
    except Exception as e:
        # Enhanced error logging for debugging
        import traceback
        error_details = f"Error in handle_stock_selection_collection: {str(e)}\nTraceback: {traceback.format_exc()}"
        try:
            frappe.log_error(error_details, "Nexchat Debug Error")
        except:
            pass
        clear_conversation_state(user)
        return f"Error processing selection: {str(e)}. Please try again or contact support."

def handle_item_details_collection(message, state, user):
    """Handle comprehensive item details collection for Stock Entry"""
    try:
        stage = state.get("stage", "item_selection")
        data = state.get("data", {})
        current_item = state.get("current_item", {})
        items_list = state.get("items_list", [])
        needs_target_warehouse = state.get("needs_target_warehouse", False)
        numbered_options = state.get("numbered_options", [])
        user_input = message.strip()
        
        # Handle cancel
        if user_input.lower() in ['cancel', 'quit', 'exit']:
            clear_conversation_state(user)
            doctype_name = state.get("data", {}).get("doctype", "Stock Entry")
            return f"{doctype_name} creation cancelled."
        
        if stage == "item_selection":
            # Handle item selection
            selected_item = None
            
            if user_input.isdigit():
                try:
                    num = int(user_input)
                    if 1 <= num <= len(numbered_options):
                        selected_item = numbered_options[num - 1]
                    else:
                        return f"❌ Invalid number: {num}. Please use numbers between 1 and {len(numbered_options)}."
                except ValueError:
                    return f"❌ Invalid input. Please use numbers or type the item code."
            else:
                # User typed item code or name
                selected_item = user_input
            
            # Store selected item and move to quantity
            current_item["item_code"] = selected_item
            
            # Update state for quantity input
            state.update({
                "stage": "quantity_input",
                "current_item": current_item
            })
            set_conversation_state(user, state)
            
            return f"📦 **Item:** {selected_item}\n\n💯 **Enter Quantity** (default: 1):\n• Type a number (e.g., `5`, `10.5`)\n• Press Enter for default (1)"
        
        elif stage == "quantity_input":
            # Handle quantity input
            if user_input == "":
                quantity = 1
            else:
                try:
                    quantity = float(user_input)
                    if quantity <= 0:
                        return "❌ Quantity must be greater than 0. Please enter a valid quantity."
                except ValueError:
                    return "❌ Invalid quantity. Please enter a number (e.g., 5, 10.5) or press Enter for default (1)."
            
            current_item["qty"] = quantity
            
            # Update state for rate input
            state.update({
                "stage": "rate_input",
                "current_item": current_item
            })
            set_conversation_state(user, state)
            
            return f"📦 **Item:** {current_item['item_code']}\n💯 **Quantity:** {quantity}\n\n💰 **Enter Basic Rate** (default: 0):\n• Type a number (e.g., `100`, `25.50`)\n• Press Enter for default (0)"
        
        elif stage == "rate_input":
            # Handle rate input
            if user_input == "":
                rate = 0
            else:
                try:
                    rate = float(user_input)
                    if rate < 0:
                        return "❌ Rate cannot be negative. Please enter a valid rate."
                except ValueError:
                    return "❌ Invalid rate. Please enter a number (e.g., 100, 25.50) or press Enter for default (0)."
            
            current_item["basic_rate"] = rate
            
            # Set source warehouse from data
            current_item["s_warehouse"] = data.get("from_warehouse") or data.get("s_warehouse")
            
            if needs_target_warehouse:
                # Move to target warehouse selection
                state.update({
                    "stage": "target_warehouse",
                    "current_item": current_item
                })
                set_conversation_state(user, state)
                
                return show_target_warehouse_selection(current_item, state, user)
            else:
                # No target warehouse needed, finalize item
                return finalize_current_item(current_item, state, user)
        
        elif stage == "target_warehouse":
            # Handle target warehouse selection
            selected_warehouse = None
            
            if user_input.isdigit():
                try:
                    warehouse_options = state.get("warehouse_options", [])
                    num = int(user_input)
                    if 1 <= num <= len(warehouse_options):
                        selected_warehouse = warehouse_options[num - 1]
                    else:
                        return f"❌ Invalid number: {num}. Please use numbers between 1 and {len(warehouse_options)}."
                except ValueError:
                    return f"❌ Invalid input. Please use numbers or type the warehouse name."
            else:
                # User typed warehouse name
                selected_warehouse = user_input
            
            current_item["t_warehouse"] = selected_warehouse
            
            # Finalize item
            return finalize_current_item(current_item, state, user)
        
        elif stage == "add_more_items":
            # Handle add more items decision
            if user_input.lower() in ['yes', 'y', '1', 'add', 'more']:
                # Reset for new item
                state.update({
                    "stage": "item_selection",
                    "current_item": {}
                })
                set_conversation_state(user, state)
                
                return show_items_selection(data, state.get("missing_fields", []), user)
            else:
                # Done adding items, create the stock entry
                return create_stock_entry_with_items(data, items_list, user)
        
        else:
            clear_conversation_state(user)
            return "Error in item collection process. Please start over."
            
    except Exception as e:
        clear_conversation_state(user)
        return f"Error processing item details: {str(e)}"

def show_target_warehouse_selection(current_item, state, user):
    """Show target warehouse selection for current item"""
    try:
        # Get available warehouses
        warehouses = frappe.get_all("Warehouse", 
                                  fields=["name", "warehouse_name"],
                                  order_by="name",
                                  limit=10)
        
        response_parts = [
            f"📦 **Item:** {current_item['item_code']}",
            f"💯 **Quantity:** {current_item['qty']}",
            f"💰 **Rate:** {current_item['basic_rate']}\n",
            "🏪 **Select Target Warehouse:**\n"
        ]
        
        warehouse_names = []
        for i, warehouse in enumerate(warehouses, 1):
            warehouse_display = warehouse.name
            if warehouse.warehouse_name and warehouse.warehouse_name != warehouse.name:
                warehouse_display += f" ({warehouse.warehouse_name})"
            response_parts.append(f"`{i}` **{warehouse_display}**")
            warehouse_names.append(warehouse.name)
        
        response_parts.extend([
            "",
            "**💡 How to select:**",
            "• Type a **number** (e.g., `2`) for your choice",
            "• Type the **warehouse name** directly"
        ])
        
        # Update state with warehouse options
        state["warehouse_options"] = warehouse_names
        set_conversation_state(user, state)
        
        return "\n".join(response_parts)
        
    except Exception as e:
        return f"Error showing target warehouse selection: {str(e)}"

def finalize_current_item(current_item, state, user):
    """Finalize current item and ask if user wants to add more"""
    try:
        items_list = state.get("items_list", [])
        items_list.append(current_item.copy())
        
        # Show summary of added item
        response_parts = [
            "✅ **Item Added Successfully!**\n",
            f"📦 **Item:** {current_item['item_code']}",
            f"💯 **Quantity:** {current_item['qty']}",
            f"💰 **Rate:** {current_item['basic_rate']}",
            f"🏪 **From:** {current_item.get('s_warehouse', 'N/A')}"
        ]
        
        if current_item.get('t_warehouse'):
            response_parts.append(f"🏪 **To:** {current_item['t_warehouse']}")
        
        response_parts.extend([
            f"\n📋 **Total Items Added:** {len(items_list)}",
            "",
            "🔄 **Add Another Item?**",
            "• Type `yes` to add another item",
            "• Type `no` to create the Stock Entry",
            "• Type `done` to finish"
        ])
        
        # Update state
        state.update({
            "stage": "add_more_items",
            "items_list": items_list,
            "current_item": {}
        })
        set_conversation_state(user, state)
        
        return "\n".join(response_parts)
        
    except Exception as e:
        return f"Error finalizing item: {str(e)}"

def create_stock_entry_with_items(data, items_list, user):
    """Create the Stock Entry with all collected items"""
    try:
        if not items_list:
            clear_conversation_state(user)
            return "❌ No items added. Stock Entry creation cancelled."
        
        # Add items to data
        data["items_list"] = items_list
        
        # Clear conversation state
        clear_conversation_state(user)
        
        # Create the stock entry
        return create_document("Stock Entry", data, user)
        
    except Exception as e:
        clear_conversation_state(user)
        return f"Error creating Stock Entry: {str(e)}"

def get_intent_from_gemini(user_input, user):
    """Use Gemini to understand user intent and convert to structured data"""
    
    # Check if Gemini is available
    if not genai:
        return {
            "reply": "Gemini AI is not available. Please install google-generativeai package."
        }
    
    # Get API key from site config
    api_key = frappe.conf.get("gemini_api_key")
    if not api_key:
        return {
            "reply": "I'm sorry, but the AI service is not configured properly. Please contact your administrator to set up the Gemini API key."
        }
    
    try:
        # Configure Gemini
        genai.configure(api_key=api_key)
        
        # Try different model names in order of preference
        model_names = ['gemini-1.5-flash', 'gemini-1.5-pro', 'gemini-pro', 'models/gemini-1.5-flash']
        model = None
        
        for model_name in model_names:
            try:
                model = genai.GenerativeModel(model_name)
                break
            except Exception as model_error:
                continue
                
        if not model:
            return {
                "reply": "AI service is temporarily unavailable. Please try again later."
            }

        # Get available doctypes for the user
        available_doctypes = get_user_accessible_doctypes()
        doctype_list = ", ".join(available_doctypes)

        # Enhanced prompt for better understanding
        prompt = f"""
You are an ERPNext assistant. Your job is to convert natural language into a JSON object.
The user '{user}' said: "{user_input}".

Available ERPNext doctypes for this user: {doctype_list}

Analyze the user's request and provide a JSON object with 'doctype', 'action', and relevant data.

Supported actions:
- "create": Create a new document
- "list": Show/list documents  
- "get": Get specific document information
- "update": Update existing document fields
- "delete": Delete a document
- "assign": Assign/link documents or roles
- "help": Provide help information

Examples:
- "Create a new customer" -> {{"doctype": "Customer", "action": "create", "data": {{}}}}
- "Create item with name Widget" -> {{"doctype": "Item", "action": "create", "data": {{"item_name": "Widget"}}}}
- "Show me all customers" -> {{"doctype": "Customer", "action": "list", "filters": {{}}}}
- "List items where item_group is Raw Material" -> {{"doctype": "Item", "action": "list", "filters": {{"item_group": "Raw Material"}}}}
- "Get customer CUST-001" -> {{"doctype": "Customer", "action": "get", "filters": {{"name": "CUST-001"}}}}
- "Update customer CUST-001 set customer_name to ABC Corp" -> {{"doctype": "Customer", "action": "update", "filters": {{"name": "CUST-001"}}, "data": {{"customer_name": "ABC Corp"}}}}
- "Change first name of user@example.com" -> {{"doctype": "User", "action": "update", "filters": {{"name": "user@example.com"}}, "field_to_update": "first_name"}}
- "Update email of customer CUST-001" -> {{"doctype": "Customer", "action": "update", "filters": {{"name": "CUST-001"}}, "field_to_update": "email"}}
- "Delete sales order SO-001" -> {{"doctype": "Sales Order", "action": "delete", "filters": {{"name": "SO-001"}}}}
- "Assign Sales User role to user@example.com" -> {{"doctype": "User", "action": "assign", "target": "user@example.com", "assign_type": "role", "value": "Sales User"}}
- "Show all roles" -> {{"action": "list_roles"}}
- "List all available roles" -> {{"action": "list_roles"}}
- "Help me with sales orders" -> {{"action": "help", "topic": "Sales Order"}}

Important:
- Only use doctypes from the available list
- For create actions, include any mentioned field values in the 'data' object
- For list/get actions, use 'filters' to specify search criteria
- For update actions: If both field and value are specified, use 'data'. If only field is mentioned, use 'field_to_update'
- For assign actions, extract the user email and role name if mentioned
- Always extract document identifiers into 'filters' for update/get/delete actions
- If the request is unclear, ask for clarification

Respond with ONLY the JSON object, no additional text or formatting.
        """

        response = model.generate_content(prompt)
        
        # Clean and parse the response
        clean_json_str = response.text.strip()
        
        # Remove markdown formatting if present
        if clean_json_str.startswith("```json"):
            clean_json_str = clean_json_str[7:]
        if clean_json_str.endswith("```"):
            clean_json_str = clean_json_str[:-3]
        clean_json_str = clean_json_str.strip()
        
        return json.loads(clean_json_str)
        
    except json.JSONDecodeError as e:
        frappe.log_error(f"Gemini JSON Parse Error: {str(e)[:80]}...", "Nexchat JSON Parse Error")
        return {
            "reply": "I had trouble understanding your request. Could you please rephrase it more clearly?"
        }
    except Exception as e:
        # Log error with shortened title to avoid character limit issues
        error_msg = str(e)[:100] + "..." if len(str(e)) > 100 else str(e)
        frappe.log_error(f"Gemini API Error: {error_msg}", "Nexchat Gemini API Error")
        return {
            "reply": "I'm having trouble processing your request right now. Please try again in a moment."
        }

def get_user_accessible_doctypes():
    """Get list of ALL doctypes the current user has access to"""
    try:
        # Get all doctypes from the system
        all_doctypes = frappe.get_all("DocType", 
                                     filters={
                                         "issingle": 0,  # Exclude single doctypes
                                         "istable": 0,   # Exclude child table doctypes
                                         "custom": 0     # Exclude custom doctypes for now
                                     },
                                     fields=["name"],
                                     order_by="name")
        
        accessible_doctypes = []
        for doctype_info in all_doctypes:
            doctype = doctype_info.name
            try:
                # Skip system/internal doctypes that users shouldn't interact with
                if doctype in ['DocType', 'DocField', 'Print Format', 'Custom Field', 
                              'Property Setter', 'Client Script', 'Server Script',
                              'Workflow', 'Workflow State', 'Workflow Action Master',
                              'Role', 'Role Profile', 'User Permission', 'DocShare',
                              'Session Default', 'DefaultValue', 'Translation']:
                    continue
                    
                if frappe.has_permission(doctype, "read"):
                    accessible_doctypes.append(doctype)
            except:
                continue
                
        return accessible_doctypes
    except:
        # Fallback to common doctypes if there's an error
        return ["Customer", "Supplier", "Item", "Sales Order", "Purchase Order",
                "Sales Invoice", "Purchase Invoice", "Lead", "Opportunity",
                "Quotation", "User", "Contact", "Address", "Task", "Project"]

def execute_task(task_json, user):
    """Execute the task based on the parsed JSON from Gemini"""
    
    action = task_json.get("action")
    doctype = task_json.get("doctype")
    
    # CRITICAL DEBUG: Log what Gemini detected
    try:
        frappe.log_error(f"Gemini detected - Action: {action}, Doctype: {doctype}, Full JSON: {task_json}", "Gemini Detection Debug")
    except:
        pass
    
    # Handle help requests
    if action == "help":
        return handle_help_request(task_json)
    
    # Handle list roles request
    if action == "list_roles":
        return handle_list_roles_request()
    
    # Handle replies (when Gemini couldn't parse the request)
    if "reply" in task_json:
        return task_json["reply"]
    
    if not action or not doctype:
        return "I'm not sure what you'd like me to do. Could you please be more specific? For example, try saying 'Create a new customer' or 'Show me my sales orders'."

    # Check permissions
    if not frappe.has_permission(doctype, "read"):
        return f"You don't have permission to access {doctype} documents."

    # Handle different actions with permission checking
    if action == "create":
        if not frappe.has_permission(doctype, "create"):
            return f"❌ You don't have permission to create {doctype} documents."
        return handle_create_action(doctype, task_json, user)
    
    elif action == "list":
        if not frappe.has_permission(doctype, "read"):
            return f"❌ You don't have permission to view {doctype} documents."
        return handle_list_action(doctype, task_json)
    
    elif action == "get":
        if not frappe.has_permission(doctype, "read"):
            return f"❌ You don't have permission to view {doctype} documents."
        return handle_get_action(doctype, task_json)
    
    elif action == "update":
        if not frappe.has_permission(doctype, "write"):
            return f"❌ You don't have permission to update {doctype} documents."
        return handle_update_action(doctype, task_json, user)
    
    elif action == "delete":
        if not frappe.has_permission(doctype, "delete"):
            return f"❌ You don't have permission to delete {doctype} documents."
        return handle_delete_action(doctype, task_json)
    
    elif action == "assign":
        return handle_assign_action(doctype, task_json, user)
    
    elif action == "assign_role":  # Keep for backward compatibility
        return handle_role_assignment(task_json, user)
    
    else:
        return f"I understand you want to work with {doctype}. I can help you:\n• **Create** new {doctype}\n• **List/View** {doctype} documents\n• **Get** specific {doctype} details\n• **Update** {doctype} fields\n• **Delete** {doctype} documents\n• **Assign** roles or links\n\nWhat would you like to do?"

def handle_create_action(doctype, task_json, user):
    """Handle document creation"""
    try:
        # CRITICAL FIX: Ensure doctype is properly preserved from the start
        try:
            frappe.log_error(f"handle_create_action called with doctype: {doctype}", "Create Action Debug")
        except:
            pass
            
        # Get required fields for the doctype
        meta = frappe.get_meta(doctype)
        required_fields = []
        
        for df in meta.fields:
            if df.reqd and not df.hidden and not df.read_only and not df.default:
                # Skip standard fields that are auto-populated
                if df.fieldname not in ['name', 'owner', 'creation', 'modified', 'modified_by', 'docstatus']:
                    # For Stock Entry, skip series as it's auto-generated
                    if doctype == "Stock Entry" and df.fieldname == "naming_series":
                        continue
                    required_fields.append(df.fieldname)

        data = task_json.get("data", {})
        missing_fields = []
        
        for field in required_fields:
            field_obj = meta.get_field(field)
            if field not in data and not field_obj.default:
                missing_fields.append(field)
        
        # For Asset doctype, also check for conditionally mandatory fields
        if doctype == "Asset":
            # gross_purchase_amount is mandatory for non-composite assets
            if "gross_purchase_amount" not in data and "gross_purchase_amount" not in missing_fields:
                missing_fields.append("gross_purchase_amount")
        
        # For Purchase Invoice, ensure company is collected even if not strictly required
        if doctype == "Purchase Invoice":
            if "company" not in data and "company" not in missing_fields:
                missing_fields.append("company")

        if missing_fields:
            
            # Special handling for Stock Entry - ALWAYS show type selection first
            if doctype == "Stock Entry":
                return show_stock_entry_type_selection(data, missing_fields, user)
            
            # For all other doctypes, use smart field selection system with proper doctype context
            field_to_ask = missing_fields[0]
            
            # Check if field exists in meta
            try:
                field_obj = meta.get_field(field_to_ask)
            except Exception as field_error:
                field_obj = None
            
            if field_obj:
                # Use smart field selection for all doctypes - CRITICAL: Pass the original doctype
                return get_smart_field_selection(field_to_ask, field_obj, data, missing_fields, user, doctype)
            else:
                # Fallback if field not found in metadata
                label_to_ask = field_to_ask.replace("_", " ").title()
                
                # Save the current state with EXPLICIT doctype - CRITICAL FIX
                state = {
                    "action": "collect_stock_selection",  # Use stock_selection for consistency
                    "selection_type": field_to_ask,
                    "doctype": doctype,  # CRITICAL: Preserve original doctype explicitly
                    "data": data,
                    "missing_fields": missing_fields,
                    "numbered_options": []
                }
                set_conversation_state(user, state)
                
                return f"I can create a {doctype} for you! What should I set as the {label_to_ask}?"
        else:
            # All required fields are present, create the document
            return create_document(doctype, data, user)
            
    except Exception as e:
        return f"Error preparing to create {doctype}: {str(e)}"

def create_document(doctype, data, user):
    """Actually create the document"""
    try:
        doc = frappe.new_doc(doctype)
        
        # Special handling for different doctypes
        if doctype == "User" and "email" in data:
            # For users, email becomes the name field
            doc.email = data["email"]
            doc.name = data["email"]
            # Set some default values for User
            doc.enabled = 1
            doc.user_type = "System User"
            # Remove email from data to avoid duplication
            user_data = data.copy()
            user_data.pop("email", None)
            doc.update(user_data)
            
        elif doctype == "Stock Entry":
            # Special handling for Stock Entry
            stock_data = data.copy()
            items_data = stock_data.pop("items", None)
            items_list = stock_data.pop("items_list", None)  # New comprehensive items list
            
            # Update basic fields first
            doc.update(stock_data)
            
            # Handle comprehensive items list (new system)
            if items_list:
                for item_data in items_list:
                    # Create item if it doesn't exist
                    item_code = item_data.get("item_code")
                    if item_code and not frappe.db.exists("Item", item_code):
                        try:
                            new_item = frappe.new_doc("Item")
                            new_item.item_code = item_code
                            new_item.item_name = item_code
                            new_item.item_group = "All Item Groups"
                            new_item.stock_uom = "Nos"
                            new_item.insert()
                            frappe.db.commit()
                        except Exception as item_error:
                            clear_conversation_state(user)
                            return f"Could not create item '{item_code}': {str(item_error)}. Please create the item first or use an existing item code."
                    
                    # Add item to stock entry with all details
                    doc.append("items", {
                        "item_code": item_data.get("item_code"),
                        "qty": item_data.get("qty", 1),
                        "basic_rate": item_data.get("basic_rate", 0),
                        "s_warehouse": item_data.get("s_warehouse"),
                        "t_warehouse": item_data.get("t_warehouse")
                    })
            
            # Handle legacy items data (fallback for old system)
            elif items_data:
                if isinstance(items_data, str):
                    # If items is a string, treat it as an item code
                    item_code = items_data
                    
                    # Determine warehouses based on stock entry type and available data
                    stock_entry_type = stock_data.get("stock_entry_type") or stock_data.get("purpose", "Material Receipt")
                    s_warehouse = stock_data.get("from_warehouse") or stock_data.get("s_warehouse")
                    t_warehouse = stock_data.get("to_warehouse") or stock_data.get("t_warehouse")
                    
                    # Get default warehouse from system if none provided
                    if not s_warehouse and not t_warehouse:
                        try:
                            default_warehouses = frappe.get_all("Warehouse", limit=1, fields=["name"])
                            default_warehouse = default_warehouses[0].name if default_warehouses else None
                        except:
                            default_warehouse = None
                    
                    # Set warehouses based on stock entry type
                    if stock_entry_type in ["Material Issue", "Material Transfer", "Send to Subcontractor"]:
                        # These types need source warehouse
                        if not s_warehouse:
                            s_warehouse = default_warehouse or "Stores"
                    elif stock_entry_type in ["Material Receipt"]:
                        # Material Receipt needs target warehouse
                        if not t_warehouse:
                            t_warehouse = default_warehouse or "Stores"
                    elif stock_entry_type in ["Material Transfer for Manufacture", "Manufacture"]:
                        # These may need both warehouses
                        if not s_warehouse:
                            s_warehouse = default_warehouse or "Stores"
                        if not t_warehouse:
                            t_warehouse = default_warehouse or "Finished Goods"
                    
                    # Check if item exists
                    if frappe.db.exists("Item", item_code):
                        doc.append("items", {
                            "item_code": item_code,
                            "qty": 1,  # Default quantity
                            "basic_rate": 0,  # Default rate
                            "s_warehouse": s_warehouse,
                            "t_warehouse": t_warehouse
                        })
                    else:
                        # Create a simple item if it doesn't exist
                        try:
                            new_item = frappe.new_doc("Item")
                            new_item.item_code = item_code
                            new_item.item_name = item_code
                            new_item.item_group = "All Item Groups"  # Default item group
                            new_item.stock_uom = "Nos"  # Default UOM
                            new_item.insert()
                            frappe.db.commit()
                            
                            # Now add to stock entry
                            doc.append("items", {
                                "item_code": item_code,
                                "qty": 1,
                                "basic_rate": 0,
                                "s_warehouse": s_warehouse,
                                "t_warehouse": t_warehouse
                            })
                        except Exception as item_error:
                            clear_conversation_state(user)
                            return f"Could not create item '{item_code}': {str(item_error)}. Please create the item first or use an existing item code."
                            
                elif isinstance(items_data, list):
                    # If items is a list, add each item
                    for item in items_data:
                        if isinstance(item, dict):
                            doc.append("items", item)
                        else:
                            doc.append("items", {
                                "item_code": str(item),
                                "qty": 1,
                                "basic_rate": 0,
                                "s_warehouse": stock_data.get("from_warehouse"),
                                "t_warehouse": stock_data.get("to_warehouse")
                            })
        else:
            # Default handling for other doctypes
            doc.update(data)
        
        # Insert the document
        doc.insert()
        frappe.db.commit()
        
        clear_conversation_state(user)
        return f"✅ {doctype} '{doc.name}' has been created successfully! You can view it in the {doctype} list."
        
    except frappe.DuplicateEntryError:
        clear_conversation_state(user)
        return f"A {doctype} with this information already exists. Please check the {doctype} list or try with different details."
    except frappe.ValidationError as e:
        clear_conversation_state(user)
        return f"Could not create {doctype}: {str(e)}"
    except Exception as e:
        clear_conversation_state(user)
        return f"Error creating {doctype}: {str(e)}"

def handle_list_action(doctype, task_json):
    """Handle listing documents"""
    try:
        filters = task_json.get("filters", {})
        
        # Get recent documents (limit to 10 for chat display)
        docs = frappe.get_list(
            doctype,
            filters=filters,
            fields=["name", "modified"],
            order_by="modified desc",
            limit=10
        )
        
        if not docs:
            filter_text = f" matching your criteria" if filters else ""
            return f"No {doctype} documents found{filter_text}."
        
        doc_list = "\n".join([f"• {doc.name}" for doc in docs])
        count_text = f"Here are the {len(docs)} most recent" if len(docs) == 10 else f"Found {len(docs)}"
        
        return f"{count_text} {doctype} documents:\n\n{doc_list}\n\nYou can view more details by asking about a specific document."
        
    except Exception as e:
        return f"Error retrieving {doctype} list: {str(e)}"

def handle_get_action(doctype, task_json):
    """Handle getting specific document information"""
    try:
        filters = task_json.get("filters", {})
        field = task_json.get("field")
        
        if not filters:
            return f"Please specify which {doctype} you'd like to get information about."
        
        doc = frappe.get_doc(doctype, filters)
        
        if field and hasattr(doc, field):
            value = getattr(doc, field)
            return f"The {field} for {doctype} '{doc.name}' is: {value}"
        else:
            # Return basic info about the document
            info_fields = ["name"]
            meta = frappe.get_meta(doctype)
            
            # Add some commonly useful fields
            for df in meta.fields[:5]:  # First few fields
                if not df.hidden and df.fieldtype not in ["Section Break", "Column Break", "HTML"]:
                    info_fields.append(df.fieldname)
            
            info = []
            for field_name in info_fields:
                if hasattr(doc, field_name):
                    value = getattr(doc, field_name)
                    if value:
                        field_label = meta.get_field(field_name).label if meta.get_field(field_name) else field_name
                        info.append(f"{field_label}: {value}")
            
            return f"Here's information about {doctype} '{doc.name}':\n\n" + "\n".join(info)
            
    except frappe.DoesNotExistError:
        return f"Could not find a {doctype} matching your criteria."
    except Exception as e:
        return f"Error retrieving {doctype} information: {str(e)}"

def handle_update_action(doctype, task_json, user):
    """Handle document updates"""
    try:
        filters = task_json.get("filters", {})
        data = task_json.get("data", {})
        field_to_update = task_json.get("field_to_update")  # For partial updates
        
        if not filters:
            return f"Please specify which {doctype} document you want to update. For example: 'Update customer CUST-001 set customer_name to New Name'"
        
        # Check if document exists
        if not frappe.db.exists(doctype, filters):
            return f"Could not find a {doctype} document matching your criteria."
        
        # Get the document to show available fields if needed
        doc = frappe.get_doc(doctype, filters)
        
        # If no data provided or incomplete, ask for missing information
        if not data and not field_to_update:
            # Show available fields for this doctype
            meta = frappe.get_meta(doctype)
            updatable_fields = []
            for df in meta.fields:
                if not df.read_only and not df.hidden and df.fieldtype not in ['Section Break', 'Column Break', 'HTML', 'Heading']:
                    if hasattr(doc, df.fieldname):
                        current_value = getattr(doc, df.fieldname) or "Not set"
                        updatable_fields.append(f"• **{df.label or df.fieldname}** (current: {current_value})")
            
            field_list = "\n".join(updatable_fields[:10])  # Show first 10 fields
            
            # Save state for field collection
            state = {
                "action": "collect_update_info",
                "doctype": doctype,
                "filters": filters,
                "doc_name": doc.name
            }
            set_conversation_state(user, state)
            
            return f"I found {doctype} '{doc.name}'. Which field would you like to update?\n\nAvailable fields:\n{field_list}\n\nPlease specify: 'Update [field_name] to [new_value]'"
        
        # If field specified but no value, ask for the value
        if field_to_update and not data:
            # Save state for value collection
            state = {
                "action": "collect_update_value",
                "doctype": doctype,
                "filters": filters,
                "doc_name": doc.name,
                "field_to_update": field_to_update
            }
            set_conversation_state(user, state)
            
            # Get current value
            current_value = getattr(doc, field_to_update) if hasattr(doc, field_to_update) else "Not set"
            field_label = frappe.get_meta(doctype).get_field(field_to_update).label or field_to_update
            
            return f"What should I set the {field_label} to? (current value: {current_value})"
        
        # Proceed with update
        if not data:
            return f"Please specify what value you want to set. For example: 'Update {doctype} {doc.name} set customer_name to New Name'"
        
        # Update the fields
        updated_fields = []
        for field, value in data.items():
            if hasattr(doc, field):
                old_value = getattr(doc, field)
                setattr(doc, field, value)
                updated_fields.append(f"{field}: '{old_value}' → '{value}'")
            else:
                # Suggest similar field names
                meta = frappe.get_meta(doctype)
                similar_fields = [df.fieldname for df in meta.fields if field.lower() in df.fieldname.lower()]
                if similar_fields:
                    suggestions = ", ".join(similar_fields[:3])
                    return f"Field '{field}' does not exist in {doctype}. Did you mean: {suggestions}?"
                else:
                    return f"Field '{field}' does not exist in {doctype}. Use 'Update {doctype} {doc.name}' to see available fields."
        
        # Save the document
        doc.save()
        frappe.db.commit()
        
        updated_list = "\n".join([f"• {field}" for field in updated_fields])
        return f"✅ Successfully updated {doctype} '{doc.name}':\n\n{updated_list}"
        
    except frappe.ValidationError as e:
        return f"Could not update {doctype}: {str(e)}"
    except frappe.PermissionError:
        return f"You don't have permission to update this {doctype} document."
    except Exception as e:
        return f"Error updating {doctype}: {str(e)}"

def handle_delete_action(doctype, task_json):
    """Handle document deletion"""
    try:
        filters = task_json.get("filters", {})
        
        if not filters:
            return f"Please specify which {doctype} document you want to delete. For example: 'Delete customer CUST-001'"
        
        # Check if document exists
        if not frappe.db.exists(doctype, filters):
            return f"Could not find a {doctype} document matching your criteria."
        
        # Get the document name for confirmation
        doc = frappe.get_doc(doctype, filters)
        doc_name = doc.name
        
        # Check if document can be deleted (not submitted)
        if hasattr(doc, 'docstatus') and doc.docstatus == 1:
            return f"Cannot delete {doctype} '{doc_name}' because it is submitted. Please cancel it first."
        
        # Delete the document
        frappe.delete_doc(doctype, doc_name)
        frappe.db.commit()
        
        return f"✅ Successfully deleted {doctype} '{doc_name}'."
        
    except frappe.LinkExistsError:
        return f"Cannot delete this {doctype} because it is linked to other documents. Please remove the links first."
    except frappe.PermissionError:
        return f"You don't have permission to delete this {doctype} document."
    except Exception as e:
        return f"Error deleting {doctype}: {str(e)}"

def handle_assign_action(doctype, task_json, current_user):
    """Handle assignment operations (roles, documents, etc.)"""
    try:
        assign_type = task_json.get("assign_type", "")
        target = task_json.get("target", "")
        value = task_json.get("value", "")
        
        # Handle role assignment (backward compatibility)
        if assign_type == "role" or doctype == "User":
            if not target:
                return "Please specify which user you want to assign a role to."
            
            if not value:
                # Ask for role selection with numbered options
                available_roles = get_available_roles()
                if available_roles:
                    return show_role_selection_interface(target, available_roles, current_user)
                else:
                    return "No roles are available for assignment."
            else:
                return assign_role_to_user(target, value)
        
        # Handle document assignment (linking)
        elif assign_type == "document":
            return f"Document linking is not yet implemented. Please use the ERPNext interface for complex document relationships."
        
        else:
            return f"Assignment type '{assign_type}' is not supported. I can assign roles to users."
            
    except Exception as e:
        return f"Error handling assignment: {str(e)}"

def handle_role_assignment(task_json, current_user):
    """Handle role assignment to users"""
    try:
        # Check if current user has permission to manage users
        if not frappe.has_permission("User", "write"):
            return "You don't have permission to assign roles to users."
        
        target_user = task_json.get("user")
        role_name = task_json.get("role")
        
        if not target_user:
            return "Please specify which user you want to assign a role to. For example: 'Assign role to user@example.com'"
        
        # Check if user exists
        if not frappe.db.exists("User", target_user):
            return f"User '{target_user}' does not exist. Please create the user first or check the email address."
        
        if not role_name:
            # Ask for role selection
            available_roles = get_available_roles()
            if available_roles:
                role_list = "\n".join([f"• {role}" for role in available_roles[:10]])
                
                # Save state for role collection
                state = {
                    "action": "collect_role",
                    "target_user": target_user,
                    "available_roles": available_roles
                }
                set_conversation_state(current_user, state)
                
                return f"Which role would you like to assign to {target_user}?\n\nAvailable roles:\n{role_list}\n\nPlease type the role name."
            else:
                return "No roles are available for assignment."
        else:
            # Role specified, assign it
            return assign_role_to_user(target_user, role_name)
            
    except Exception as e:
        return f"Error handling role assignment: {str(e)}"

def get_available_roles():
    """Get list of available roles"""
    try:
        roles = frappe.get_all("Role", 
                              filters={"disabled": 0}, 
                              fields=["name"],
                              order_by="name")
        return [role.name for role in roles if not role.name.startswith("Guest")]
    except:
        return ["System Manager", "Sales User", "Purchase User", "HR User", "Accounts User"]

def show_role_selection_interface(target_user, available_roles, current_user):
    """Display role selection interface with numbered options"""
    try:
        # Group roles by category for better organization
        system_roles = []
        user_roles = []
        other_roles = []
        
        for role in available_roles:
            if "Manager" in role or "Administrator" in role or role in ["System Manager", "Website Manager"]:
                system_roles.append(role)
            elif "User" in role or role in ["Sales User", "Purchase User", "HR User", "Accounts User"]:
                user_roles.append(role)
            else:
                other_roles.append(role)
        
        # Create numbered role list with beautiful circular badges
        numbered_roles = []
        role_sections = []
        current_number = 1
        # Unicode circled numbers for beautiful badges
        circled_numbers = ["①", "②", "③", "④", "⑤", "⑥", "⑦", "⑧", "⑨", "⑩", "⑪", "⑫", "⑬", "⑭", "⑮", "⑯", "⑰", "⑱", "⑲", "⑳"]
        
        if system_roles:
            role_sections.append("**🔧 System & Management Roles:**")
            for role in sorted(system_roles):
                numbered_roles.append(role)
                badge = circled_numbers[current_number-1] if current_number <= len(circled_numbers) else f"({current_number})"
                role_sections.append(f"{badge} **{role}**")
                current_number += 1
            role_sections.append("")
        
        if user_roles:
            role_sections.append("**👤 User Roles:**")
            for role in sorted(user_roles):
                numbered_roles.append(role)
                badge = circled_numbers[current_number-1] if current_number <= len(circled_numbers) else f"({current_number})"
                role_sections.append(f"{badge} **{role}**")
                current_number += 1
            role_sections.append("")
        
        if other_roles:
            role_sections.append("**📂 Other Roles:**")
            for role in sorted(other_roles):
                numbered_roles.append(role)
                badge = circled_numbers[current_number-1] if current_number <= len(circled_numbers) else f"({current_number})"
                role_sections.append(f"{badge} **{role}**")
                current_number += 1
            role_sections.append("")
        
        # Save state for role collection
        state = {
            "action": "collect_role_selection",
            "target_user": target_user,
            "available_roles": available_roles,
            "numbered_roles": numbered_roles
        }
        set_conversation_state(current_user, state)
        
        # Create the response
        response_parts = [
            f"🎯 **Select Role(s) for {target_user}**\n",
            "\n".join(role_sections),
            "**💡 How to select:**",
            "• Type a **number** (e.g., `5`) for single role",
            "• Type **multiple numbers** with commas (e.g., `1,3,7`) for multiple roles",
            "• Type the **role name** directly",
            "• Type `all roles` or `*` to assign **ALL** available roles",
            "• Type `all` to see full list with descriptions",
            "• Type `cancel` to cancel\n",
            f"📝 **Examples:**",
            f"• `1,5,8` → Assign specific roles",
            f"• `all roles` or `*` → Assign ALL {len(numbered_roles)} roles",
            f"• `Sales User` → Assign by name"
        ]
        
        return "\n".join(response_parts)
        
    except Exception as e:
        return f"Error displaying role selection: {str(e)}"

def assign_role_to_user(user_email, role_name):
    """Actually assign the role to the user"""
    try:
        # Check if role exists
        if not frappe.db.exists("Role", role_name):
            return f"Role '{role_name}' does not exist. Please check the role name."
        
        # Check if user already has this role
        existing_role = frappe.db.exists("Has Role", {
            "parent": user_email,
            "role": role_name
        })
        
        if existing_role:
            return f"User '{user_email}' already has the '{role_name}' role."
        
        # Add the role
        user_doc = frappe.get_doc("User", user_email)
        user_doc.append("roles", {
            "role": role_name
        })
        user_doc.save()
        frappe.db.commit()
        
        return f"✅ Successfully assigned '{role_name}' role to user '{user_email}'!"
        
    except frappe.PermissionError:
        return f"You don't have permission to assign the '{role_name}' role."
    except Exception as e:
        return f"Error assigning role: {str(e)}"

def assign_multiple_roles_to_user(user_email, role_names):
    """Assign multiple roles to a user"""
    try:
        user_doc = frappe.get_doc("User", user_email)
        
        assigned_roles = []
        already_assigned = []
        failed_roles = []
        
        for role_name in role_names:
            try:
                # Check if role exists
                if not frappe.db.exists("Role", role_name):
                    failed_roles.append(f"{role_name} (doesn't exist)")
                    continue
                
                # Check if user already has this role
                existing_role = frappe.db.exists("Has Role", {
                    "parent": user_email,
                    "role": role_name
                })
                
                if existing_role:
                    already_assigned.append(role_name)
                else:
                    # Add the role
                    user_doc.append("roles", {
                        "role": role_name
                    })
                    assigned_roles.append(role_name)
                    
            except Exception as e:
                failed_roles.append(f"{role_name} (error: {str(e)})")
        
        # Save if any roles were added
        if assigned_roles:
            user_doc.save()
            frappe.db.commit()
        
        # Build response message
        response_parts = []
        
        if assigned_roles:
            response_parts.append(f"✅ **Successfully assigned {len(assigned_roles)} role(s) to '{user_email}':**")
            for role in assigned_roles:
                response_parts.append(f"   • {role}")
        
        if already_assigned:
            response_parts.append(f"\n📋 **Already assigned ({len(already_assigned)} role(s)):**")
            for role in already_assigned:
                response_parts.append(f"   • {role}")
        
        if failed_roles:
            response_parts.append(f"\n❌ **Failed to assign ({len(failed_roles)} role(s)):**")
            for role in failed_roles:
                response_parts.append(f"   • {role}")
        
        if not response_parts:
            return f"No changes made to user '{user_email}' roles."
        
        return "\n".join(response_parts)
        
    except frappe.PermissionError:
        return f"You don't have permission to assign roles to users."
    except Exception as e:
        return f"Error assigning multiple roles: {str(e)}"

def assign_all_roles_to_user(user_email, available_roles):
    """Assign ALL available roles to a user with confirmation"""
    try:
        # Filter out dangerous roles that shouldn't be auto-assigned
        excluded_roles = [
            'Guest',
            'Website Manager',  # Could be dangerous for security
            'System Manager'    # Should be assigned carefully
        ]
        
        # Ask user if they want to include system roles
        system_roles = ['System Manager', 'Website Manager', 'Administrator']
        
        safe_roles = [role for role in available_roles if role not in excluded_roles]
        sensitive_roles = [role for role in available_roles if role in system_roles and role in available_roles]
        
        # Get current user doc to see existing roles
        user_doc = frappe.get_doc("User", user_email)
        current_roles = [role.role for role in user_doc.get("roles", [])]
        
        # Calculate roles to assign
        roles_to_assign = [role for role in safe_roles if role not in current_roles]
        sensitive_to_assign = [role for role in sensitive_roles if role not in current_roles]
        
        if not roles_to_assign and not sensitive_to_assign:
            return f"🎯 User '{user_email}' already has all available roles!\n\n📋 **Current roles:** {len(current_roles)}\n• " + "\n• ".join(current_roles)
        
        # Assign all safe roles
        assigned_count = 0
        assigned_roles = []
        failed_roles = []
        
        for role_name in roles_to_assign:
            try:
                user_doc.append("roles", {
                    "role": role_name
                })
                assigned_roles.append(role_name)
                assigned_count += 1
            except Exception as e:
                failed_roles.append(f"{role_name} (error: {str(e)})")
        
        # Save changes
        if assigned_roles:
            user_doc.save()
            frappe.db.commit()
        
        # Build comprehensive response
        response_parts = [
            f"🎉 **ALL ROLES ASSIGNED to '{user_email}'!**\n",
            f"✅ **Successfully assigned {len(assigned_roles)} new role(s):**"
        ]
        
        # Group assigned roles by category
        system_assigned = [r for r in assigned_roles if 'Manager' in r or 'Administrator' in r]
        user_assigned = [r for r in assigned_roles if 'User' in r]
        other_assigned = [r for r in assigned_roles if r not in system_assigned and r not in user_assigned]
        
        if system_assigned:
            response_parts.append("   🔧 **System & Management:**")
            for role in system_assigned:
                response_parts.append(f"      • {role}")
        
        if user_assigned:
            response_parts.append("   👤 **User Roles:**")
            for role in user_assigned:
                response_parts.append(f"      • {role}")
        
        if other_assigned:
            response_parts.append("   📂 **Other Roles:**")
            for role in other_assigned:
                response_parts.append(f"      • {role}")
        
        # Show sensitive roles that were skipped
        if sensitive_to_assign:
            response_parts.append(f"\n⚠️  **High-privilege roles NOT auto-assigned ({len(sensitive_to_assign)}):**")
            response_parts.append("   (Assign these manually for security)")
            for role in sensitive_to_assign:
                response_parts.append(f"      • {role}")
        
        # Show already assigned count
        already_had = len(current_roles)
        if already_had > 0:
            response_parts.append(f"\n📋 **Already had {already_had} role(s)** (kept unchanged)")
        
        # Show failures if any
        if failed_roles:
            response_parts.append(f"\n❌ **Failed to assign ({len(failed_roles)}):**")
            for role in failed_roles:
                response_parts.append(f"      • {role}")
        
        # Final summary
        total_roles_now = len(current_roles) + len(assigned_roles)
        response_parts.append(f"\n📊 **SUMMARY:**")
        response_parts.append(f"   • Total roles now: **{total_roles_now}**")
        response_parts.append(f"   • Newly assigned: **{len(assigned_roles)}**")
        response_parts.append(f"   • User '{user_email}' now has comprehensive access! 🚀")
        
        return "\n".join(response_parts)
        
    except frappe.PermissionError:
        return f"❌ You don't have permission to assign roles to users."
    except Exception as e:
        return f"❌ Error assigning all roles: {str(e)}"

def handle_list_roles_request():
    """Handle request to list all available roles"""
    try:
        # Check if user has permission to view roles
        if not frappe.has_permission("Role", "read"):
            return "❌ You don't have permission to view roles."
        
        all_roles = get_available_roles()
        if not all_roles:
            return "No roles are available."
        
        # Group roles by category if possible
        system_roles = []
        user_roles = []
        other_roles = []
        
        for role in all_roles:
            if "Manager" in role or "Administrator" in role or role in ["System Manager", "Website Manager"]:
                system_roles.append(role)
            elif "User" in role or role in ["Sales User", "Purchase User", "HR User", "Accounts User"]:
                user_roles.append(role)
            else:
                other_roles.append(role)
        
        # Format the response
        response_parts = [f"📋 **All Available Roles** ({len(all_roles)} total)\n"]
        
        if system_roles:
            response_parts.append("**🔧 System & Management Roles:**")
            response_parts.append("\n".join([f"• {role}" for role in sorted(system_roles)]))
            response_parts.append("")
        
        if user_roles:
            response_parts.append("**👤 User Roles:**")
            response_parts.append("\n".join([f"• {role}" for role in sorted(user_roles)]))
            response_parts.append("")
        
        if other_roles:
            response_parts.append("**📂 Other Roles:**")
            response_parts.append("\n".join([f"• {role}" for role in sorted(other_roles)]))
            response_parts.append("")
        
        response_parts.append("💡 **Usage:** `assign [role_name] role to [user@email.com]`")
        
        return "\n".join(response_parts)
        
    except Exception as e:
        return f"Error retrieving roles: {str(e)}"

def handle_help_request(task_json):
    """Handle help requests"""
    topic = task_json.get("topic", "").lower()
    
    if "customer" in topic:
        return """🏢 **Customer Management Help**

I can help you with:
• **Create a customer**: "Create a new customer"
• **List customers**: "Show me all customers" 
• **Find a customer**: "Get customer information for [name]"

Customers are used to track your clients and are required for creating sales orders and invoices."""
    
    elif "sales" in topic or "order" in topic:
        return """📋 **Sales Order Help**

I can help you with:
• **Create a sales order**: "Create a sales order for customer [name]"
• **List sales orders**: "Show me recent sales orders"
• **Find an order**: "Get sales order [number]"

Sales orders track customer purchases and can be converted to invoices."""
    
    else:
        return """🤖 **Nexchat Help**

I'm your ERPNext AI assistant! I can help you with **ALL** ERPNext documents:

**📝 CREATE Documents**
• "Create a new customer"
• "Create item with name Widget"
• "Make a sales order for ABC Corp"

**📊 READ/LIST Information**  
• "Show me all customers"
• "List items where item_group is Raw Material"
• "Get customer CUST-001"

**✏️ UPDATE Documents**
• "Update customer CUST-001 set customer_name to New Name"
• "Update item ITEM-001 set item_group to Finished Goods"

**🗑️ DELETE Documents**
• "Delete customer CUST-001"
• "Delete sales order SO-001"

**🔗 ASSIGN Roles/Links**
• "Assign Sales User role to user@company.com"
• "Give System Manager role to admin@company.com"

**💡 Tips**
• I work with **ANY** ERPNext doctype (Customer, Item, Sales Order, Purchase Order, Employee, etc.)
• I respect your user permissions - you can only perform actions you're authorized for
• Be specific about document names/IDs for updates and deletions
• I'll ask for required information if needed

**🔐 Permission-Aware**
All operations check your ERPNext permissions automatically!

Try: "Create a new [doctype]" or "List all [doctype]" with any ERPNext document type!"""

def show_stock_entry_type_selection(data, missing_fields, user):
    """Show interactive selection for Stock Entry Type"""
    try:
        # Always use the standard stock entry types to ensure consistency
        stock_entry_types = [
            "Material Issue",
            "Material Receipt", 
            "Material Transfer",
            "Material Transfer for Manufacture",
            "Manufacture",
            "Repack",
            "Send to Subcontractor"
        ]
        
        # Group stock entry types by category for better organization
        inbound_types = ["Material Receipt"]
        outbound_types = ["Material Issue", "Send to Subcontractor"]
        transfer_types = ["Material Transfer", "Material Transfer for Manufacture"]
        production_types = ["Manufacture", "Repack"]
        
        # Create formatted list with categorization
        response_parts = [
            "🎯 **Select Stock Entry Type:**\n"
        ]
        
        current_number = 1
        
        # Unicode circled numbers for beautiful badges
        circled_numbers = ["①", "②", "③", "④", "⑤", "⑥", "⑦", "⑧", "⑨", "⑩", "⑪", "⑫", "⑬", "⑭", "⑮", "⑯", "⑰", "⑱", "⑲", "⑳"]
        
        # Inbound Operations
        response_parts.append("**📥 Inbound Operations:**")
        for entry_type in inbound_types:
            response_parts.append(f"{circled_numbers[current_number-1]} **{entry_type}**")
            current_number += 1
        response_parts.append("")
        
        # Outbound Operations  
        response_parts.append("**📤 Outbound Operations:**")
        for entry_type in outbound_types:
            response_parts.append(f"{circled_numbers[current_number-1]} **{entry_type}**")
            current_number += 1
        response_parts.append("")
        
        # Transfer Operations
        response_parts.append("**🔄 Transfer Operations:**")
        for entry_type in transfer_types:
            response_parts.append(f"{circled_numbers[current_number-1]} **{entry_type}**")
            current_number += 1
        response_parts.append("")
        
        # Production Operations
        response_parts.append("**🏭 Production Operations:**")
        for entry_type in production_types:
            response_parts.append(f"{circled_numbers[current_number-1]} **{entry_type}**")
            current_number += 1
        response_parts.append("")
        
        response_parts.extend([
            "**💡 How to select:**",
            "• Type a **number** (e.g., `2`) for your choice",
            "• Type the **operation name** directly",
            "• Type `cancel` to cancel",
            "",
            "**📝 Examples:**",
            "• `2` → Material Receipt",
            "• `Material Transfer` → By name",
            "",
            "**ℹ️ Operation Types:**",
            "• **Inbound:** Receive materials into warehouse",
            "• **Outbound:** Issue materials from warehouse", 
            "• **Transfer:** Move materials between warehouses",
            "• **Production:** Manufacturing & repackaging operations"
        ])
        
        # Find the actual field name for stock entry type from missing fields
        actual_field_name = "stock_entry_type"  # default
        for field_name in missing_fields:
            if field_name in ["stock_entry_type", "purpose", "voucher_type"] or "type" in field_name.lower() or "purpose" in field_name.lower():
                actual_field_name = field_name
                break
        
        # If no type field found, use the first missing field (we'll populate it with stock entry type)
        if actual_field_name == "stock_entry_type" and missing_fields:
            actual_field_name = missing_fields[0]
        
        state = {
            "action": "collect_stock_selection",
            "selection_type": actual_field_name,
            "data": data,
            "missing_fields": missing_fields,
            "numbered_options": stock_entry_types
        }
        set_conversation_state(user, state)
        
        return "\n".join(response_parts)
        
    except Exception as e:
        return f"Error showing stock entry type selection: {str(e)}"

def show_company_selection(data, missing_fields, user, current_doctype=None):
    """Show interactive selection for Company"""
    try:
        # Get available companies
        companies = frappe.get_all("Company", 
                                 fields=["name"],
                                 order_by="name")
        
        company_names = [comp.name for comp in companies]
        
        if not company_names:
            # If no companies found, ask for manual input
            # Use the provided doctype or try to determine from data
            doctype_for_meta = current_doctype or "Stock Entry"
            meta = frappe.get_meta(doctype_for_meta)
            field_obj = meta.get_field("company")
            label_to_ask = field_obj.label or "company"
            
            state = {
                "action": "collect_fields",
                "doctype": current_doctype or "Stock Entry",
                "data": data,
                "missing_fields": missing_fields
            }
            set_conversation_state(user, state)
            
            return f"What should I set as the {label_to_ask}?"
        
        # Create formatted list with better styling
        response_parts = [
            "🏢 **Select Company:**\n"
        ]
        
        # Unicode circled numbers for beautiful badges
        circled_numbers = ["①", "②", "③", "④", "⑤", "⑥", "⑦", "⑧", "⑨", "⑩", "⑪", "⑫", "⑬", "⑭", "⑮", "⑯", "⑰", "⑱", "⑲", "⑳"]
        
        total_companies = len(company_names)
        for i, company in enumerate(company_names, 1):
            badge = circled_numbers[i-1] if i <= len(circled_numbers) else f"({i})"
            response_parts.append(f"{badge} **{company}**")
        
        response_parts.extend([
            "",
            f"📊 **Total Companies:** {total_companies}",
            "",
            "**💡 How to select:**",
            "• Type a **number** (e.g., `1`) for your choice",
            "• Type the **company name** directly",
            "• Type `cancel` to cancel",
            "",
            "**📝 Examples:**",
            f"• `1` → {company_names[0] if company_names else 'First Company'}",
            f"• `{company_names[0] if company_names else 'Company Name'}` → By name"
        ])
        
        # Determine the doctype - prefer provided parameter, then detect from data
        if current_doctype:
            final_doctype = current_doctype
        else:
            # CRITICAL: Check naming series FIRST before other detection
            final_doctype = None
            if data.get("naming_series"):
                series = data.get("naming_series", "")
                if "ACC-PINV-" in series:
                    final_doctype = "Purchase Invoice"
                elif "PUR-ORD-" in series:
                    final_doctype = "Purchase Order"
                elif "ACC-SINV-" in series:
                    final_doctype = "Sales Invoice"
                elif "SO-" in series:
                    final_doctype = "Sales Order"
            
            # Only use field-based detection if naming series didn't work
            if not final_doctype:
                # Intelligent doctype detection based on data patterns
                if "supplier" in data:
                    # Could be Purchase Order or Purchase Invoice
                    if any(field in data for field in ["bill_no", "bill_date", "due_date"]):
                        final_doctype = "Purchase Invoice"
                    else:
                        final_doctype = "Purchase Order"
                elif "customer" in data:
                    # Could be Sales Order, Sales Invoice, or Quotation
                    if any(field in data for field in ["due_date", "posting_date"]) and "delivery_date" not in data:
                        final_doctype = "Sales Invoice"
                    elif "valid_till" in data:
                        final_doctype = "Quotation"
                    else:
                        final_doctype = "Sales Order"
                elif "item_code" in data and any(field in data for field in ["location", "gross_purchase_amount"]):
                    final_doctype = "Asset"
                elif "stock_entry_type" in data or "purpose" in data or any(field in data for field in ["from_warehouse", "to_warehouse", "s_warehouse", "t_warehouse"]):
                    final_doctype = "Stock Entry"
                else:
                    # Last resort fallback
                    final_doctype = "Stock Entry"
            
        state = {
            "action": "collect_stock_selection",
            "selection_type": "company",
            "doctype": final_doctype,
            "data": data,
            "missing_fields": missing_fields,
            "numbered_options": company_names
        }
        set_conversation_state(user, state)
        
        return "\n".join(response_parts)
        
    except Exception as e:
        return f"Error showing company selection: {str(e)}"

def show_items_selection(data, missing_fields, user):
    """Show interactive selection for Items with comprehensive details"""
    try:
        # Get available items
        items = frappe.get_all("Item", 
                             fields=["item_code", "item_name"],
                             order_by="item_code")
        
        # Check if we need target warehouse for this stock entry type
        stock_entry_type = data.get("stock_entry_type") or data.get("purpose", "")
        needs_target_warehouse = stock_entry_type in ["Material Receipt", "Material Transfer", "Material Transfer for Manufacture", "Manufacture"]
        
        response_parts = [
            "📦 **Add Item to Stock Entry:**\n"
        ]
        
        if items:
            response_parts.append("**📦 Available Items:**")
            item_codes = []
            # Unicode circled numbers for beautiful badges
            circled_numbers = ["①", "②", "③", "④", "⑤", "⑥", "⑦", "⑧", "⑨", "⑩", "⑪", "⑫", "⑬", "⑭", "⑮", "⑯", "⑰", "⑱", "⑲", "⑳"]
            
            for i, item in enumerate(items, 1):
                item_display = f"**{item.item_code}**"
                if item.item_name and item.item_name != item.item_code:
                    item_display += f" - _{item.item_name}_"
                badge = circled_numbers[i-1] if i <= len(circled_numbers) else f"({i})"
                response_parts.append(f"{badge} {item_display}")
                item_codes.append(item.item_code)
            
            response_parts.extend([
                "",
                f"📊 **Total Items:** {len(items)}",
                "",
                "**💡 How to select:**",
                "• Type a **number** (e.g., `1`) for your choice",
                "• Type an **item code** directly",
                "• Type a **new item name** to create it",
                "• Type `cancel` to cancel",
                "",
                "**📝 Examples:**",
                f"• `1` → {items[0].item_code if items else 'First Item'}",
                f"• `{items[0].item_code if items else 'ITEM-001'}` → By item code",
                "• `New Widget` → Create new item"
            ])
        else:
            response_parts.extend([
                "**ℹ️ No items found in system.**",
                "",
                "**💡 How to select:**",
                "• Type an **item code** or **new item name**",
                "• Type `cancel` to cancel",
                "",
                "**📝 Note:** If the item doesn't exist, I'll help you create it."
            ])
            item_codes = []
        
        # Show what we'll collect next
        response_parts.extend([
            "**📋 After selecting item, I'll ask for:**",
            "• Quantity (default: 1)",
            "• Basic Rate (default: 0)"
        ])
        
        if needs_target_warehouse:
            response_parts.append("• Target Warehouse")
        
        response_parts.append(f"\n**Current Stock Entry Type:** {stock_entry_type}")
        
        # Save state for item entry
        state = {
            "action": "collect_item_details",
            "stage": "item_selection",
            "data": data,
            "missing_fields": missing_fields,
            "numbered_options": item_codes,
            "current_item": {},
            "needs_target_warehouse": needs_target_warehouse,
            "items_list": data.get("items_list", [])
        }
        set_conversation_state(user, state)
        
        return "\n".join(response_parts)
        
    except Exception as e:
        return f"Error showing items selection: {str(e)}"

def show_warehouse_selection(field_name, data, missing_fields, user):
    """Show interactive selection for Warehouse fields"""
    try:
        # Get available warehouses
        warehouses = frappe.get_all("Warehouse", 
                                  fields=["name", "warehouse_name"],
                                  order_by="name")
        
        if not warehouses:
            # If no warehouses found, ask for manual input
            meta = frappe.get_meta("Stock Entry")
            field_obj = meta.get_field(field_name)
            label_to_ask = field_obj.label or field_name
            
            state = {
                "action": "collect_fields",
                "doctype": "Stock Entry",
                "data": data,
                "missing_fields": missing_fields
            }
            set_conversation_state(user, state)
            
            return f"What should I set as the {label_to_ask}?"
        
        # Get field label for display
        meta = frappe.get_meta("Stock Entry")
        field_obj = meta.get_field(field_name)
        field_label = field_obj.label or field_name.replace("_", " ").title()
        
        # Create formatted list with better styling
        response_parts = [
            f"🏪 **Select {field_label}:**\n"
        ]
        
        # Group warehouses by type if possible (you can extend this logic)
        response_parts.append("**📦 Available Warehouses:**")
        
        # Unicode circled numbers for beautiful badges
        circled_numbers = ["①", "②", "③", "④", "⑤", "⑥", "⑦", "⑧", "⑨", "⑩", "⑪", "⑫", "⑬", "⑭", "⑮", "⑯", "⑰", "⑱", "⑲", "⑳"]
        
        warehouse_names = []
        for i, warehouse in enumerate(warehouses, 1):
            warehouse_display = f"**{warehouse.name}**"
            if warehouse.warehouse_name and warehouse.warehouse_name != warehouse.name:
                warehouse_display += f" - _{warehouse.warehouse_name}_"
            badge = circled_numbers[i-1] if i <= len(circled_numbers) else f"({i})"
            response_parts.append(f"{badge} {warehouse_display}")
            warehouse_names.append(warehouse.name)
        
        response_parts.extend([
            "",
            f"📊 **Total Warehouses:** {len(warehouses)}",
            "",
            "**💡 How to select:**",
            "• Type a **number** (e.g., `1`) for your choice",
            "• Type the **warehouse name** directly", 
            "• Type `cancel` to cancel",
            "",
            "**📝 Examples:**",
            f"• `1` → {warehouse_names[0] if warehouse_names else 'First Warehouse'}",
            f"• `{warehouse_names[0] if warehouse_names else 'Stores'}` → By name"
        ])
        
        # Save state
        state = {
            "action": "collect_stock_selection",
            "selection_type": field_name,
            "data": data,
            "missing_fields": missing_fields,
            "numbered_options": warehouse_names
        }
        set_conversation_state(user, state)
        
        return "\n".join(response_parts)
        
    except Exception as e:
        return f"Error showing warehouse selection: {str(e)}" 

def show_asset_item_selection(data, missing_fields, user):
    """Show interactive selection for Asset Item Code"""
    try:
        # Get items that can be assets (is_fixed_asset = 1)
        items = frappe.get_all("Item", 
                             filters={"is_fixed_asset": 1},
                             fields=["item_code", "item_name"],
                             order_by="item_code")
        
        if not items:
            # If no asset items found, show all items
            items = frappe.get_all("Item", 
                                 fields=["item_code", "item_name"],
                                 order_by="item_code")
        
        response_parts = [
            "🏭 **Select Asset Item:**\n"
        ]
        
        if items:
            item_codes = []
            # Unicode circled numbers for beautiful badges
            circled_numbers = ["①", "②", "③", "④", "⑤", "⑥", "⑦", "⑧", "⑨", "⑩", "⑪", "⑫", "⑬", "⑭", "⑮", "⑯", "⑰", "⑱", "⑲", "⑳"]
            
            for i, item in enumerate(items, 1):
                item_display = f"{item.item_code}"
                if item.item_name and item.item_name != item.item_code:
                    item_display += f" ({item.item_name})"
                badge = circled_numbers[i-1] if i <= len(circled_numbers) else f"({i})"
                response_parts.append(f"{badge} **{item_display}**")
                item_codes.append(item.item_code)
            
            response_parts.extend([
                "",
                "**💡 How to select:**",
                "• Type a **number** (e.g., `1`) for your choice",
                "• Type the **item code** directly",
                "• Type `cancel` to cancel\n",
                f"**📝 Showing first {len(items)} items.**"
            ])
        else:
            response_parts.extend([
                "No items found in system.",
                "• Type an **item code** directly",
                "• Type `cancel` to cancel"
            ])
            item_codes = []
        
        # Save state - determine doctype from context  
        current_doctype = "Asset"  # this function is specifically for Asset items
            
        state = {
            "action": "collect_stock_selection",
            "selection_type": "item_code",
            "doctype": current_doctype,
            "data": data,
            "missing_fields": missing_fields,
            "numbered_options": item_codes
        }
        set_conversation_state(user, state)
        
        return "\n".join(response_parts)
        
    except Exception as e:
        return f"Error showing asset item selection: {str(e)}"

def show_location_selection(data, missing_fields, user):
    """Show interactive selection for Asset Location"""
    try:
        # Get available locations
        locations = frappe.get_all("Location", 
                                 fields=["name", "location_name"],
                                 order_by="name")
        
        response_parts = [
            "📍 **Select Asset Location:**\n"
        ]
        
        if locations:
            location_names = []
            # Unicode circled numbers for beautiful badges
            circled_numbers = ["①", "②", "③", "④", "⑤", "⑥", "⑦", "⑧", "⑨", "⑩", "⑪", "⑫", "⑬", "⑭", "⑮", "⑯", "⑰", "⑱", "⑲", "⑳"]
            
            for i, location in enumerate(locations, 1):
                location_display = location.name
                if location.location_name and location.location_name != location.name:
                    location_display += f" ({location.location_name})"
                badge = circled_numbers[i-1] if i <= len(circled_numbers) else f"({i})"
                response_parts.append(f"{badge} **{location_display}**")
                location_names.append(location.name)
            
            response_parts.extend([
                "",
                "**💡 How to select:**",
                "• Type a **number** (e.g., `1`) for your choice",
                "• Type the **location name** directly",
                "• Type `new location name` to create it",
                "• Type `cancel` to cancel\n",
                f"**📝 Showing first {len(locations)} locations. You can also create new ones.**"
            ])
        else:
            response_parts.extend([
                "No locations found in system.",
                "• Type a **location name** to create it",
                "• Type `cancel` to cancel"
            ])
            location_names = []
        
        # Save state - determine doctype from context
        current_doctype = "Asset"  # this function is specifically for Asset location
            
        state = {
            "action": "collect_stock_selection",
            "selection_type": "location",
            "doctype": current_doctype,
            "data": data,
            "missing_fields": missing_fields,
            "numbered_options": location_names
        }
        set_conversation_state(user, state)
        
        return "\n".join(response_parts)
        
    except Exception as e:
        return f"Error showing location selection: {str(e)}"

def show_asset_field_selection(field_name, data, missing_fields, user):
    """Show interactive selection for Asset fields like asset_category, asset_owner"""
    try:
        field_data = None
        field_label = field_name.replace("_", " ").title()
        
        if field_name == "asset_category":
            # Get asset categories
            field_data = frappe.get_all("Asset Category", 
                                      fields=["name", "asset_category_name"],
                                      order_by="name")
            field_label = "Asset Category"
            icon = "🏷️"
        elif field_name == "asset_owner":
            # Get employees or users who can own assets
            field_data = frappe.get_all("Employee", 
                                      fields=["name", "employee_name"],
                                      order_by="name")
            field_label = "Asset Owner"
            icon = "👤"
        
        response_parts = [
            f"{icon} **Select {field_label}:**\n"
        ]
        
        if field_data:
            field_options = []
            # Unicode circled numbers for beautiful badges
            circled_numbers = ["①", "②", "③", "④", "⑤", "⑥", "⑦", "⑧", "⑨", "⑩", "⑪", "⑫", "⑬", "⑭", "⑮", "⑯", "⑰", "⑱", "⑲", "⑳"]
            
            for i, item in enumerate(field_data, 1):
                item_display = item.name
                # Use the second field as display name if available
                if len(item) > 1:
                    second_field = list(item.values())[1]
                    if second_field and second_field != item.name:
                        item_display += f" ({second_field})"
                badge = circled_numbers[i-1] if i <= len(circled_numbers) else f"({i})"
                response_parts.append(f"{badge} **{item_display}**")
                field_options.append(item.name)
            
            response_parts.extend([
                "",
                "**💡 How to select:**",
                "• Type a **number** (e.g., `1`) for your choice",
                "• Type the **name** directly",
                "• Type `cancel` to cancel\n",
                f"**📝 Showing first {len(field_data)} {field_label.lower()}s.**"
            ])
        else:
            response_parts.extend([
                f"No {field_label.lower()}s found in system.",
                "• Type a **name** directly",
                "• Type `cancel` to cancel"
            ])
            field_options = []
        
        # Save state - determine doctype from context
        current_doctype = "Asset"  # this function is specifically for Asset fields
            
        state = {
            "action": "collect_stock_selection",
            "selection_type": field_name,
            "doctype": current_doctype,
            "data": data,
            "missing_fields": missing_fields,
            "numbered_options": field_options
        }
        set_conversation_state(user, state)
        
        return "\n".join(response_parts)
        
    except Exception as e:
        return f"Error showing {field_label} selection: {str(e)}"

def show_asset_purchase_amount_selection(data, missing_fields, user):
    """Show input interface for Asset Gross Purchase Amount"""
    try:
        response_parts = [
            "💰 **Enter Net Purchase Amount:**\n",
            "This is the cost at which the asset was purchased.\n",
            "**💡 How to enter:**",
            "• Type the amount (e.g., `50000`, `25000.50`)",
            "• Type `0` if no purchase amount",
            "• Type `cancel` to cancel\n",
            "**📝 Examples:**",
            "• `50000` → ₹50,000",
            "• `25000.50` → ₹25,000.50"
        ]
        
        # Save state - determine doctype from context
        current_doctype = "Asset"  # this function is specifically for Asset purchase amount
            
        state = {
            "action": "collect_stock_selection",
            "selection_type": "gross_purchase_amount",
            "doctype": current_doctype,
            "data": data,
            "missing_fields": missing_fields,
            "numbered_options": []  # No numbered options for amount input
        }
        set_conversation_state(user, state)
        
        return "\n".join(response_parts)
        
    except Exception as e:
        return f"Error showing purchase amount selection: {str(e)}"

def show_generic_link_selection(field_name, field_label, link_doctype, data, missing_fields, user, current_doctype):
    """Show interactive selection for any Link field"""
    try:
        # Get available records for the link doctype
        records = frappe.get_all(link_doctype, 
                                fields=["name"],
                                order_by="name")
        
        # Try to get a better display field
        link_meta = frappe.get_meta(link_doctype)
        display_field = None
        for field in ["title", "full_name", "employee_name", "customer_name", "supplier_name", "item_name"]:
            if link_meta.get_field(field):
                display_field = field
                break
        
        if display_field:
            records = frappe.get_all(link_doctype, 
                                   fields=["name", display_field],
                                   order_by="name")
        
        # Create appropriate icon based on doctype
        icons = {
            "Company": "🏢", "Customer": "👤", "Supplier": "🏭", "Item": "📦",
            "Employee": "👨‍💼", "User": "👤", "Currency": "💱", "Cost Center": "🏦",
            "Project": "📋", "Task": "✅", "Lead": "🎯", "Opportunity": "💰",
            "Quotation": "📝", "Sales Order": "📊", "Purchase Order": "🛒",
            "Sales Invoice": "🧾", "Purchase Invoice": "📄", "Location": "📍",
            "Warehouse": "🏪", "UOM": "📏", "Item Group": "📂", "Brand": "🏷️"
        }
        icon = icons.get(link_doctype, "🔗")
        
        response_parts = [
            f"{icon} **Select {field_label}:**\n"
        ]
        
        if records:
            response_parts.append(f"**📋 Available {link_doctype}s:**")
            record_names = []
            total_records = len(records)
            
            # Unicode circled numbers for beautiful badges
            circled_numbers = ["①", "②", "③", "④", "⑤", "⑥", "⑦", "⑧", "⑨", "⑩", "⑪", "⑫", "⑬", "⑭", "⑮", "⑯", "⑰", "⑱", "⑲", "⑳"]
            
            # Always use single column with better formatting
            for i, record in enumerate(records, 1):
                record_display = f"**{record.name}**"
                if display_field and record.get(display_field) and record.get(display_field) != record.name:
                    record_display += f" - _{record.get(display_field)}_"
                badge = circled_numbers[i-1] if i <= len(circled_numbers) else f"({i})"
                response_parts.append(f"{badge} {record_display}")
                record_names.append(record.name)
            
            response_parts.extend([
                "",
                f"📊 **Total {link_doctype}s:** {total_records}",
                "",
                "**💡 How to select:**",
                "• Type a **number** (e.g., `1`) for your choice",
                "• Type the **name** directly",
                "• Type `cancel` to cancel",
                "",
                "**📝 Examples:**",
                f"• `1` → {record_names[0] if record_names else f'First {link_doctype}'}",
                f"• `{record_names[0] if record_names else 'Name'}` → By name"
            ])
        else:
            response_parts.extend([
                f"**ℹ️ No {link_doctype.lower()}s found in system.**",
                "",
                "**💡 How to select:**",
                "• Type a **name** directly",
                "• Type `cancel` to cancel"
            ])
            record_names = []
        
        # Save state
        state = {
            "action": "collect_stock_selection",
            "selection_type": field_name,
            "doctype": current_doctype,
            "data": data,
            "missing_fields": missing_fields,
            "numbered_options": record_names
        }
        set_conversation_state(user, state)
        
        return "\n".join(response_parts)
        
    except Exception as e:
        return f"Error showing {field_label} selection: {str(e)}"

def show_generic_select_selection(field_name, field_label, options, data, missing_fields, user, current_doctype):
    """Show interactive selection for any Select field"""
    try:
        # Parse options (they come as newline-separated string)
        option_list = [opt.strip() for opt in options.split('\n') if opt.strip()]
        
        # Remove empty first option if present
        if option_list and option_list[0] == '':
            option_list = option_list[1:]
        
        response_parts = [
            f"⚙️ **Select {field_label}:**\n"
        ]
        
        # Add context description
        response_parts.append(f"**📝 Field Required:** {field_label} for {current_doctype}")
        response_parts.append("")
        
        if option_list:
            response_parts.append("**📋 Available Options:**")
            # Unicode circled numbers for beautiful badges
            circled_numbers = ["①", "②", "③", "④", "⑤", "⑥", "⑦", "⑧", "⑨", "⑩", "⑪", "⑫", "⑬", "⑭", "⑮", "⑯", "⑰", "⑱", "⑲", "⑳"]
            
            for i, option in enumerate(option_list, 1):
                badge = circled_numbers[i-1] if i <= len(circled_numbers) else f"({i})"
                response_parts.append(f"{badge} **{option}**")
            
            response_parts.extend([
                "",
                f"📊 **Total Options:** {len(option_list)}",
                "",
                "**💡 How to select:**",
                "• Type a **number** (e.g., `1`) for your choice",
                "• Type the **option name** directly",
                "• Type `cancel` to cancel",
                "",
                "**📝 Examples:**",
                f"• `1` → {option_list[0] if option_list else 'First Option'}",
                f"• `{option_list[0] if option_list else 'Option Name'}` → By name"
            ])
        else:
            response_parts.extend([
                "**ℹ️ No options available for this field.**",
                "",
                "**💡 How to proceed:**",
                "• Type `cancel` to cancel",
                "• Contact administrator to configure options"
            ])
        
        # Save state
        state = {
            "action": "collect_stock_selection",
            "selection_type": field_name,
            "doctype": current_doctype,
            "data": data,
            "missing_fields": missing_fields,
            "numbered_options": option_list
        }
        set_conversation_state(user, state)
        
        return "\n".join(response_parts)
        
    except Exception as e:
        return f"Error showing {field_label} selection: {str(e)}"

def show_generic_currency_selection(field_name, field_label, data, missing_fields, user, current_doctype):
    """Show beautifully styled currency input interface using markdown"""
    try:
        response_parts = [
            f"💰 **Enter {field_label}**\n",
            f"📝 **Field Required:** {field_label} for {current_doctype}\n",
            "**💡 How to enter:**",
            "• Type the amount as a number",
            "• Use decimal point for cents (e.g., `25000.50`)",
            "• Type `0` if no amount",
            "• Type `cancel` to cancel",
            "",
            "**📝 Examples:**",
            "• `50000` → ₹50,000",
            "• `25000.50` → ₹25,000.50",
            "• `100.99` → ₹100.99",
            "• `0` → No amount",
            "",
            "**ℹ️ Supported formats:**",
            "• Whole numbers: `1000`, `50000`",
            "• Decimals: `1000.50`, `25.99`",
            "• Large amounts: `1000000` (1 million)"
        ]
        
        # Save state
        state = {
            "action": "collect_stock_selection",
            "selection_type": field_name,
            "field_type": "Currency",
            "doctype": current_doctype,
            "data": data,
            "missing_fields": missing_fields,
            "numbered_options": []
        }
        set_conversation_state(user, state)
        
        return "\n".join(response_parts)
        
    except Exception as e:
        return f"Error showing {field_label} input: {str(e)}"

def show_generic_numeric_selection(field_name, field_label, fieldtype, data, missing_fields, user, current_doctype):
    """Show beautifully styled numeric input interface using markdown"""
    try:
        # Create appropriate icon and examples based on field type
        if fieldtype == "Int":
            icon = "🔢"
            examples = ["• `100` → 100", "• `250` → 250", "• `1000` → 1,000"]
            description = f"whole number for {field_label.lower()}"
            formats = ["• Positive numbers: `100`, `250`", "• Zero: `0`", "• No negative values allowed"]
        elif fieldtype == "Percent":
            icon = "📊" 
            examples = ["• `15` → 15%", "• `25.5` → 25.5%", "• `100` → 100%"]
            description = f"percentage value for {field_label.lower()}"
            formats = ["• Whole percent: `15`, `50`", "• Decimal percent: `25.5`, `12.75`", "• Range: 0 to 100"]
        else:  # Float
            icon = "💯"
            examples = ["• `100.50` → 100.50", "• `25.75` → 25.75", "• `1000.99` → 1,000.99"]
            description = f"decimal number for {field_label.lower()}"
            formats = ["• Decimals: `100.50`, `25.75`", "• Whole numbers: `100`, `250`", "• Scientific: `1e3` (1000)"]
        
        response_parts = [
            f"{icon} **Enter {field_label}**\n",
            f"📝 **Field Required:** {field_label} for {current_doctype}\n",
            "**💡 How to enter:**",
            f"• Type a {description}",
            "• Type `0` if no value",
            "• Type `cancel` to cancel",
            "",
            "**📝 Examples:**"
        ]
        response_parts.extend(examples)
        response_parts.extend([
            "",
            "**ℹ️ Supported formats:**"
        ])
        response_parts.extend(formats)
        
        # Save state
        state = {
            "action": "collect_stock_selection",
            "selection_type": field_name,
            "field_type": fieldtype,
            "doctype": current_doctype,
            "data": data,
            "missing_fields": missing_fields,
            "numbered_options": []
        }
        set_conversation_state(user, state)
        
        return "\n".join(response_parts)
        
    except Exception as e:
        return f"Error showing {field_label} input: {str(e)}"

def show_generic_date_selection(field_name, field_label, data, missing_fields, user, current_doctype):
    """Show beautifully styled date selection interface using markdown"""
    try:
        from datetime import date, timedelta
        
        today = date.today()
        tomorrow = today + timedelta(days=1)
        week_later = today + timedelta(days=7)
        month_later = today + timedelta(days=30)
        
        # Create response using markdown formatting
        response_parts = [
            f"📅 **Select {field_label}**\n",
            f"📝 **Field Required:** {field_label} for {current_doctype}\n",
            "**🗓️ Quick Date Options:**"
        ]
        
        # Add quick date options with circular badges
        circled_numbers = ["①", "②", "③", "④"]
        quick_options = [
            (f"**Today** - {today.strftime('%Y-%m-%d')}", today.strftime('%A')),
            (f"**Tomorrow** - {tomorrow.strftime('%Y-%m-%d')}", tomorrow.strftime('%A')),
            (f"**Next Week** - {week_later.strftime('%Y-%m-%d')}", week_later.strftime('%A')),
            (f"**Next Month** - {month_later.strftime('%Y-%m-%d')}", month_later.strftime('%A'))
        ]
        
        for i, (option_text, day_name) in enumerate(quick_options):
            response_parts.append(f"{circled_numbers[i]} {option_text} ({day_name})")
        
        response_parts.extend([
            "",
            "**💡 How to select:**",
            "• Type a **number** (e.g., `1`) for quick options",
            "• Type a **custom date** in YYYY-MM-DD format",
            "• Type `cancel` to cancel",
            "",
            "**📝 Examples:**",
            "• `1` → Today",
            "• `2024-12-25` → Christmas Day",
            "• `2024-06-15` → June 15th, 2024",
            "• `2024-03-01` → March 1st, 2024",
            "",
            "**ℹ️ Date format requirements:**",
            "• Must use YYYY-MM-DD format",
            "• Year: 4 digits (e.g., 2024)",
            "• Month: 2 digits (01-12)",
            "• Day: 2 digits (01-31)"
        ])
        
        date_options = [
            today.strftime("%Y-%m-%d"),
            tomorrow.strftime("%Y-%m-%d"), 
            week_later.strftime("%Y-%m-%d"),
            month_later.strftime("%Y-%m-%d")
        ]
        
        # Save state
        state = {
            "action": "collect_stock_selection",
            "selection_type": field_name,
            "field_type": "Date",
            "doctype": current_doctype,
            "data": data,
            "missing_fields": missing_fields,
            "numbered_options": date_options
        }
        set_conversation_state(user, state)
        
        return "\n".join(response_parts)
        
    except Exception as e:
        return f"Error showing {field_label} selection: {str(e)}"

def show_generic_text_input(field_name, field_label, data, missing_fields, user, current_doctype):
    """Show beautifully styled text input interface using markdown like role selection"""
    try:
        # Get appropriate icon based on field type
        field_icons = {
            "email": "📧",
            "phone": "📱", 
            "mobile": "📱",
            "name": "👤",
            "title": "📝",
            "description": "📄",
            "address": "📍",
            "website": "🌐",
            "company": "🏢"
        }
        
        # Find appropriate icon
        icon = "✏️"  # default
        for key, emoji in field_icons.items():
            if key in field_name.lower():
                icon = emoji
                break
        
        # Create response using markdown formatting like role selection
        response_parts = [
            f"{icon} **Enter {field_label}**\n",
            f"📝 **Field Required:** {field_label} for {current_doctype}\n",
            "**💡 How to enter:**"
        ]
        
        # Add specific instructions based on field type
        if "email" in field_name.lower():
            response_parts.extend([
                "• Type a valid email address",
                "• Type `cancel` to cancel",
                "",
                "**📝 Examples:**",
                "• `john.doe@company.com`",
                "• `admin@example.org`",
                "• `user123@domain.co.in`",
                "",
                "**ℹ️ Requirements:**",
                "• Must contain @ symbol",
                "• Must be a valid email format"
            ])
        elif "phone" in field_name.lower() or "mobile" in field_name.lower():
            response_parts.extend([
                "• Type a phone number",
                "• Type `cancel` to cancel",
                "",
                "**📝 Examples:**",
                "• `+91 9876543210`",
                "• `+1 555-123-4567`",
                "• `9876543210`",
                "",
                "**ℹ️ Formats supported:**",
                "• With country code: +91 9876543210",
                "• Without country code: 9876543210",
                "• With dashes: 98765-43210"
            ])
        elif "name" in field_name.lower():
            if current_doctype == "User":
                response_parts.extend([
                    "• Type the person's full name",
                    "• Type `cancel` to cancel",
                    "",
                    "**📝 Examples:**",
                    "• `John Doe`",
                    "• `Mary Johnson`",
                    "• `Dr. Sarah Smith`"
                ])
            elif current_doctype in ["Customer", "Supplier"]:
                response_parts.extend([
                    "• Type the company or person name",
                    "• Type `cancel` to cancel",
                    "",
                    "**📝 Examples:**",
                    "• `ABC Corporation`",
                    "• `XYZ Suppliers Ltd`",
                    "• `John's Trading Co`"
                ])
            else:
                response_parts.extend([
                    f"• Type the name for this {field_label.lower()}",
                    "• Type `cancel` to cancel",
                    "",
                    "**📝 Examples:**",
                    "• `John Doe`",
                    "• `ABC Corporation`"
                ])
        elif "address" in field_name.lower():
            response_parts.extend([
                "• Type the complete address",
                "• Type `cancel` to cancel",
                "",
                "**📝 Examples:**",
                "• `123 Main Street, City, State, 12345`",
                "• `Building A, Tech Park, Bangalore 560001`"
            ])
        elif "website" in field_name.lower():
            response_parts.extend([
                "• Type the website URL",
                "• Type `cancel` to cancel",
                "",
                "**📝 Examples:**",
                "• `https://www.company.com`",
                "• `www.example.org`",
                "• `company.co.in`"
            ])
        else:
            # Generic text input
            response_parts.extend([
                "• Type your text directly",
                "• Type `cancel` to cancel",
                "",
                "**📝 Example:**",
                f"• `Your {field_label.lower()} here`"
            ])
        
        # Save state
        state = {
            "action": "collect_stock_selection",
            "selection_type": field_name,
            "doctype": current_doctype,
            "data": data,
            "missing_fields": missing_fields,
            "numbered_options": []
        }
        set_conversation_state(user, state)
        
        return "\n".join(response_parts)
        
    except Exception as e:
        return f"Error showing {field_label} input: {str(e)}"

def get_smart_field_selection(field_name, field_obj, data, missing_fields, user, current_doctype):
    """Route to appropriate selection interface based on field type"""
    try:
        # Debug logging
        try:
            frappe.log_error(f"Smart field: {field_name}, {current_doctype}", "Smart Field")
        except:
            pass
            
        field_label = field_obj.label or field_name.replace("_", " ").title()
        fieldtype = field_obj.fieldtype
        
        # Handle different field types with smart interfaces
        if fieldtype == "Link":
            # Special handling for common link fields
            link_doctype = field_obj.options
            
            if field_name == "company":
                return show_company_selection(data, missing_fields, user, current_doctype)
            elif link_doctype == "Customer":
                return show_generic_link_selection(field_name, field_label, "Customer", data, missing_fields, user, current_doctype)
            elif link_doctype == "Supplier":
                return show_generic_link_selection(field_name, field_label, "Supplier", data, missing_fields, user, current_doctype)
            elif link_doctype == "Item":
                if current_doctype == "Asset":
                    return show_asset_item_selection(data, missing_fields, user)
                else:
                    return show_generic_link_selection(field_name, field_label, "Item", data, missing_fields, user, current_doctype)
            elif link_doctype == "Employee":
                return show_generic_link_selection(field_name, field_label, "Employee", data, missing_fields, user, current_doctype)
            elif field_name == "location" and current_doctype == "Asset":
                return show_location_selection(data, missing_fields, user)
            else:
                return show_generic_link_selection(field_name, field_label, link_doctype, data, missing_fields, user, current_doctype)
        
        elif fieldtype == "Select":
            options = field_obj.options or ""
            return show_generic_select_selection(field_name, field_label, options, data, missing_fields, user, current_doctype)
        
        elif fieldtype == "Currency":
            return show_generic_currency_selection(field_name, field_label, data, missing_fields, user, current_doctype)
        
        elif fieldtype == "Date":
            return show_generic_date_selection(field_name, field_label, data, missing_fields, user, current_doctype)
        
        elif fieldtype in ["Data", "Text", "Small Text", "Long Text", "Code", "HTML Editor"]:
            return show_generic_text_input(field_name, field_label, data, missing_fields, user, current_doctype)
        
        elif fieldtype in ["Int", "Float", "Percent"]:
            return show_generic_numeric_selection(field_name, field_label, fieldtype, data, missing_fields, user, current_doctype)
        
        else:
            # Fallback to generic text input
            return show_generic_text_input(field_name, field_label, data, missing_fields, user, current_doctype)
            
    except Exception as e:
        return f"Error creating smart field selection for {field_name}: {str(e)}"

def show_transaction_items_selection(doctype, data, missing_fields, user):
    """Show interactive selection for Items in transaction documents (Purchase Order, Sales Order, etc.)"""
    try:
        # Get available items
        items = frappe.get_all("Item", 
                             fields=["item_code", "item_name", "standard_rate"],
                             order_by="item_code")
        
        # Create appropriate icons and labels based on doctype
        icons = {
            "Purchase Order": "🛒",
            "Sales Order": "📊", 
            "Purchase Invoice": "📄",
            "Sales Invoice": "🧾",
            "Quotation": "📝"
        }
        icon = icons.get(doctype, "📦")
        
        response_parts = [
            f"{icon} **Add Item to {doctype}:**\n"
        ]
        
        if items:
            response_parts.append("**📦 Available Items:**")
            total_items = len(items)
            
            # Unicode circled numbers for beautiful badges
            circled_numbers = ["①", "②", "③", "④", "⑤", "⑥", "⑦", "⑧", "⑨", "⑩", "⑪", "⑫", "⑬", "⑭", "⑮", "⑯", "⑰", "⑱", "⑲", "⑳"]
            
            # Always use single column with better formatting
            for i, item in enumerate(items, 1):
                item_display = f"**{item.item_code}**"
                if item.item_name and item.item_name != item.item_code:
                    item_display += f" - _{item.item_name}_"
                badge = circled_numbers[i-1] if i <= len(circled_numbers) else f"({i})"
                response_parts.append(f"{badge} {item_display}")
            
            response_parts.append("")
            
            item_codes = [item.item_code for item in items]
            
            # Show what fields will be collected based on doctype
            fields_to_collect = []
            if doctype == "Purchase Order":
                fields_to_collect = [
                    "• Quantity (default: 1)",
                    "• Required By Date (mandatory)",
                    "• Rate (will fetch standard rate if available)",
                    "• UOM (auto-filled from item)"
                ]
            elif doctype == "Purchase Invoice":
                fields_to_collect = [
                    "• Accepted Qty (mandatory)",
                    "• Rate (auto-filled from item/supplier rates)",
                    "• UOM (auto-filled from item)"
                ]
            else:
                fields_to_collect = [
                    "• Quantity (default: 1)",
                    "• Rate (will fetch standard rate if available)"
                ]
            
            response_parts.extend([
                f"📊 **Total Items:** {total_items}",
                "",
                "**💡 How to select:**",
                "• Type a **number** (e.g., `1`) for your choice",
                "• Type the **item code** directly",
                "• Type `cancel` to cancel",
                "",
                f"**📋 After selecting item, I'll ask for:**"
            ])
            response_parts.extend([f"• {field}" for field in fields_to_collect])
            response_parts.extend([
                f"• Then create the {doctype}",
                "",
                "**📝 Examples:**",
                f"• `1` → {items[0].item_code if items else 'First Item'}",
                f"• `{items[0].item_code if items else 'ITEM-001'}` → By item code"
            ])
        else:
            response_parts.extend([
                "**ℹ️ No items found in system.**",
                "",
                "**💡 How to select:**",
                "• Type an **item code** directly",
                "• Type `cancel` to cancel",
                "",
                "**📝 Note:** If the item doesn't exist, I'll help you create it."
            ])
            item_codes = []
        
        # Save state for transaction item collection
        state = {
            "action": "collect_transaction_item_details",
            "stage": "item_selection",
            "doctype": doctype,
            "data": data,
            "missing_fields": missing_fields,
            "numbered_options": item_codes,
            "current_item": {},
            "items_list": data.get("items_list", [])
        }
        set_conversation_state(user, state)
        
        return "\n".join(response_parts)
        
    except Exception as e:
        return f"Error showing {doctype} items selection: {str(e)}"

def handle_transaction_item_details_collection(message, state, user):
    """Handle item details collection for transaction documents"""
    try:
        stage = state.get("stage", "item_selection")
        doctype = state.get("doctype", "Purchase Order")
        data = state.get("data", {})
        current_item = state.get("current_item", {})
        items_list = state.get("items_list", [])
        numbered_options = state.get("numbered_options", [])
        user_input = message.strip()
        
        # Handle cancel
        if user_input.lower() in ['cancel', 'quit', 'exit']:
            clear_conversation_state(user)
            return f"{doctype} creation cancelled."
        
        if stage == "item_selection":
            # Handle item selection
            selected_item = None
            
            if user_input.isdigit():
                try:
                    num = int(user_input)
                    if 1 <= num <= len(numbered_options):
                        selected_item = numbered_options[num - 1]
                    else:
                        return f"❌ Invalid number: {num}. Please use numbers between 1 and {len(numbered_options)}."
                except ValueError:
                    return f"❌ Invalid input. Please use numbers or type the item code."
            else:
                # User typed item code or name
                selected_item = user_input
            
            # Check if item exists and get its details
            item_doc = None
            if frappe.db.exists("Item", selected_item):
                item_doc = frappe.get_doc("Item", selected_item)
            
            # Store selected item and move to quantity
            current_item["item_code"] = selected_item
            current_item["item_name"] = item_doc.item_name if item_doc else selected_item
            current_item["rate"] = item_doc.standard_rate if item_doc and item_doc.standard_rate else 0
            # Set default UOM from item
            current_item["uom"] = item_doc.stock_uom if item_doc and item_doc.stock_uom else "Nos"
            current_item["stock_uom"] = item_doc.stock_uom if item_doc and item_doc.stock_uom else "Nos"
            
            # Update state for quantity input
            state.update({
                "stage": "quantity_input",
                "current_item": current_item
            })
            set_conversation_state(user, state)
            
            # Show different quantity prompt based on doctype
            if doctype == "Purchase Invoice":
                return f"📦 **Item:** {selected_item}\n\n💯 **Enter Accepted Qty** (default: 1):\n💡 **How to enter:** Type a number (e.g., `5`, `10.5`) or press Enter for default"
            else:
                return f"📦 **Item:** {selected_item}\n\n💯 **Enter Quantity** (default: 1):\n💡 **How to enter:** Type a number (e.g., `5`, `10.5`) or press Enter for default"
        
        elif stage == "quantity_input":
            # Handle quantity input
            if user_input == "":
                quantity = 1
            else:
                try:
                    quantity = float(user_input)
                    if quantity <= 0:
                        return "❌ Quantity must be greater than 0. Please enter a valid quantity."
                except ValueError:
                    return "❌ Invalid quantity. Please enter a number (e.g., 5, 10.5) or press Enter for default (1)."
            
            current_item["qty"] = quantity
            
            # For Purchase Invoice, skip Required By date and rate (auto-filled) - go directly to summary
            if doctype == "Purchase Invoice":
                # Set a default rate of 0 (will be auto-filled by ERPNext)
                current_item["rate"] = 0
                current_item["amount"] = 0  # Will be calculated by ERPNext
                
                # Add item to items list
                items_list.append(current_item.copy())
                
                # Show summary and ask if user wants to add more items
                response_parts = [
                    "✅ **Item Added Successfully!**",
                    "",
                    f"📦 **Item:** {current_item['item_code']}",
                    f"💯 **Accepted Qty:** {current_item['qty']}",
                    f"📏 **UOM:** {current_item.get('uom', 'Nos')}",
                    f"💰 **Rate:** Auto-filled from item/supplier rates",
                    f"",
                    f"📋 **Total Items Added:** {len(items_list)}",
                    "",
                    "🔄 **Add Another Item?**",
                    f"• Type `yes` to add another item to {doctype}",
                    f"• Type `no` to create the {doctype}",
                    "• Type `done` to finish"
                ]
                
                # Update state
                state.update({
                    "stage": "add_more_items",
                    "items_list": items_list,
                    "current_item": {}
                })
                set_conversation_state(user, state)
                
                return "\n".join(response_parts)
            
            # For Purchase Order, collect Required By date next
            elif doctype == "Purchase Order":
                state.update({
                    "stage": "required_by_input",
                    "current_item": current_item
                })
                set_conversation_state(user, state)
                
                # Show Required By date selection
                from datetime import date, timedelta
                today = date.today()
                week_later = today + timedelta(days=7)
                month_later = today + timedelta(days=30)
                
                return f"📦 **Item:** {current_item['item_code']}\n💯 **Quantity:** {quantity}\n\n📅 **Enter Required By Date:**\n\n**Quick Options:**\n  `1` Next Week ({week_later.strftime('%Y-%m-%d')})\n  `2` Next Month ({month_later.strftime('%Y-%m-%d')})\n\n💡 **How to enter:** Type `1` or `2` for quick options, or enter date as YYYY-MM-DD (e.g., `2024-12-31`)"
            else:
                # For other doctypes, go directly to rate input
                state.update({
                    "stage": "rate_input",
                    "current_item": current_item
                })
                set_conversation_state(user, state)
                
                suggested_rate = current_item.get("rate", 0)
                return f"📦 **Item:** {current_item['item_code']}\n💯 **Quantity:** {quantity}\n\n💰 **Enter Rate** (suggested: {suggested_rate}):\n💡 **How to enter:** Type a number (e.g., `100`, `25.50`) or press Enter for suggested rate"
        
        elif stage == "required_by_input":
            # Handle Required By date input for Purchase Order
            from datetime import date, timedelta, datetime
            
            selected_date = None
            today = date.today()
            week_later = today + timedelta(days=7)
            month_later = today + timedelta(days=30)
            
            if user_input == "1":
                selected_date = week_later.strftime("%Y-%m-%d")
            elif user_input == "2":
                selected_date = month_later.strftime("%Y-%m-%d")
            else:
                # Validate date format
                import re
                if re.match(r'^\d{4}-\d{2}-\d{2}$', user_input):
                    try:
                        # Validate the date and ensure it's not in the past
                        input_date = datetime.strptime(user_input, '%Y-%m-%d').date()
                        if input_date < today:
                            return "❌ Required By date cannot be in the past. Please enter a future date."
                        selected_date = user_input
                    except ValueError:
                        return "❌ Invalid date. Please use YYYY-MM-DD format (e.g., 2024-12-31) or select option 1 or 2."
                else:
                    return "❌ Invalid date format. Please use YYYY-MM-DD format (e.g., 2024-12-31) or select option 1 or 2."
            
            current_item["schedule_date"] = selected_date
            
            # Move to rate input
            state.update({
                "stage": "rate_input",
                "current_item": current_item
            })
            set_conversation_state(user, state)
            
            suggested_rate = current_item.get("rate", 0)
            return f"📦 **Item:** {current_item['item_code']}\n💯 **Quantity:** {current_item['qty']}\n📅 **Required By:** {selected_date}\n\n💰 **Enter Rate** (suggested: {suggested_rate}):\n💡 **How to enter:** Type a number (e.g., `100`, `25.50`) or press Enter for suggested rate"
        
        elif stage == "rate_input":
            # Handle rate input
            if user_input == "":
                rate = current_item.get("rate", 0)
            else:
                try:
                    rate = float(user_input)
                    if rate < 0:
                        return "❌ Rate cannot be negative. Please enter a valid rate."
                except ValueError:
                    return "❌ Invalid rate. Please enter a number (e.g., 100, 25.50) or press Enter for suggested rate."
            
            current_item["rate"] = rate
            
            # Calculate amount
            amount = current_item["qty"] * rate
            current_item["amount"] = amount
            
            # Add item to items list
            items_list.append(current_item.copy())
            
            # Show summary and ask if user wants to add more items
            response_parts = [
                "✅ **Item Added Successfully!**",
                "",
                f"📦 **Item:** {current_item['item_code']}",
                f"💯 **Quantity:** {current_item['qty']}",
                f"💰 **Rate:** {current_item['rate']}",
                f"💵 **Amount:** {amount}"
            ]
            
            # Show Required By date for Purchase Orders
            if doctype == "Purchase Order" and current_item.get("schedule_date"):
                response_parts.append(f"📅 **Required By:** {current_item['schedule_date']}")
            
            # Show UOM if available
            if current_item.get("uom"):
                response_parts.append(f"📏 **UOM:** {current_item['uom']}")
            
            response_parts.extend([
                f"",
                f"📋 **Total Items Added:** {len(items_list)}",
                "",
                "🔄 **Add Another Item?**",
                f"• Type `yes` to add another item to {doctype}",
                f"• Type `no` to create the {doctype}",
                "• Type `done` to finish"
            ])
            
            # Update state
            state.update({
                "stage": "add_more_items",
                "items_list": items_list,
                "current_item": {}
            })
            set_conversation_state(user, state)
            
            return "\n".join(response_parts)
        
        elif stage == "add_more_items":
            # Handle add more items decision
            if user_input.lower() in ['yes', 'y', '1', 'add', 'more']:
                # Reset for new item
                state.update({
                    "stage": "item_selection",
                    "current_item": {}
                })
                set_conversation_state(user, state)
                
                return show_transaction_items_selection(doctype, data, state.get("missing_fields", []), user)
            else:
                # Done adding items, create the document
                return create_transaction_document_with_items(doctype, data, items_list, user)
        
        else:
            clear_conversation_state(user)
            return f"Error in {doctype} item collection process. Please start over."
            
    except Exception as e:
        clear_conversation_state(user)
        return f"Error processing {doctype} item details: {str(e)}"

def create_transaction_document_with_items(doctype, data, items_list, user):
    """Create transaction document with collected items"""
    try:
        if not items_list:
            clear_conversation_state(user)
            return f"❌ No items added. {doctype} creation cancelled."
        
        # Add items to data in the correct format for transaction documents
        data["items"] = []
        for item in items_list:
            # Create proper item entry for transaction documents
            item_entry = {
                "item_code": item["item_code"],
                "item_name": item.get("item_name", item["item_code"]),
                "qty": item["qty"],
                "rate": item["rate"],
                "amount": item.get("amount", item["qty"] * item["rate"])
            }
            
            # Add UOM and stock_uom (mandatory for Purchase Order)
            item_entry["uom"] = item.get("uom", "Nos")
            item_entry["stock_uom"] = item.get("stock_uom", "Nos")
            
            # Add schedule_date (Required By) for Purchase Order (mandatory)
            if doctype == "Purchase Order":
                item_entry["schedule_date"] = item.get("schedule_date")
                if not item_entry["schedule_date"]:
                    # Set default to next week if somehow missing
                    from datetime import date, timedelta
                    next_week = date.today() + timedelta(days=7)
                    item_entry["schedule_date"] = next_week.strftime("%Y-%m-%d")
            
            # Add UOM if available from item master
            if frappe.db.exists("Item", item["item_code"]):
                try:
                    item_doc = frappe.get_doc("Item", item["item_code"])
                    if not item_entry.get("uom"):
                        item_entry["uom"] = item_doc.stock_uom or "Nos"
                    if not item_entry.get("stock_uom"):
                        item_entry["stock_uom"] = item_doc.stock_uom or "Nos"
                    
                    # For Purchase documents, include description
                    if doctype in ["Purchase Order", "Purchase Invoice"]:
                        item_entry["description"] = item_doc.description or item_doc.item_name
                except Exception:
                    # If we can't get item doc, use defaults
                    pass
            
            data["items"].append(item_entry)
        
        # Ensure we have transaction_date for Purchase Order (mandatory field)
        if doctype == "Purchase Order" and "transaction_date" not in data:
            from datetime import date
            data["transaction_date"] = date.today().strftime("%Y-%m-%d")
        
        # Ensure we have posting_date for Purchase Invoice (mandatory field)  
        if doctype == "Purchase Invoice" and "posting_date" not in data:
            from datetime import date
            data["posting_date"] = date.today().strftime("%Y-%m-%d")
        
        # Clear conversation state
        clear_conversation_state(user)
        
        # Create the document
        return create_document(doctype, data, user)
        
    except Exception as e:
        clear_conversation_state(user)
        return f"Error creating {doctype}: {str(e)}"

@frappe.whitelist()
def clear_user_conversation_state(user_email=None):
    """Clear conversation state for a user (for debugging)"""
    if not user_email:
        user_email = frappe.session.user
    
    clear_conversation_state(user_email)
    return f"Conversation state cleared for {user_email}"