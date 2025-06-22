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

            response = handle_stock_selection_collection(message, state, user)

        elif state.get("action") == "collect_child_table":
            response = handle_child_table_collection(message, state, user)
        elif state.get("action") == "collect_child_table_field":
            response = handle_child_table_field_input(message, state, user)
        elif state.get("action") == "collect_update_info":
            response = handle_update_info_collection(message, state, user)
        elif state.get("action") == "collect_update_value":
            response = handle_update_value_collection(message, state, user)
        else:
            # This is a new request, send it to Gemini for intent recognition
            json_response = get_intent_from_gemini(message, user)
            
            # Execute the task based on Gemini's understanding
            response = execute_task(json_response, user, message)

        return {"response": response}
    
    except Exception as e:
        # Enhanced error logging for debugging
        import traceback
        error_msg = str(e)[:200] + "..." if len(str(e)) > 200 else str(e)
        full_error = f"Nexchat Error: {error_msg}\nUser: {user}\nMessage: {message}\nTraceback: {traceback.format_exc()}"
        frappe.log_error(full_error, "Nexchat Processing Error")
        # Create beautiful error response with heavy markdown styling
        error_msg = str(e)[:200] + "..." if len(str(e)) > 200 else str(e)
        response_parts = [
            "💥 **Nexchat Processing Error**",
            "*An unexpected error occurred while processing your request*\n",
            "**🚨 Error Details:**",
            f"• `{error_msg}`",
            "",
            "**💡 What to try:**",
            "• **Retry:** Try your request again",
            "• **Rephrase:** Use different wording or approach",
            "• **Simplify:** Break complex requests into smaller parts",
            "",
            "**🔧 Troubleshooting:**",
            "• Check your request format and spelling",
            "• Ensure you have proper permissions",
            "• Try a basic command like 'help' or 'show all customers'",
            "",
            "**📞 Support:**",
            "• Contact your system administrator if the error persists",
            "• Report this error for system improvement",
            "• Check ERPNext logs for detailed technical information"
        ]
        return {"response": "\n".join(response_parts)}

# --- Generic Child Table Support ---
def get_required_child_tables(doctype):
    """Get list of required child tables for a doctype"""
    try:
        meta = frappe.get_meta(doctype)
        required_child_tables = []
        
        for df in meta.fields:
            if df.fieldtype == "Table" and df.reqd:
                required_child_tables.append(df.fieldname)
        
        return required_child_tables
    except Exception as e:
        frappe.log_error(f"Error getting required child tables for {doctype}: {str(e)}", "Child Table Error")
        return []

def get_child_table_fields(child_doctype):
    """Get required fields for a child table doctype"""
    try:
        meta = frappe.get_meta(child_doctype)
        required_fields = []
        optional_fields = []
        
        for df in meta.fields:
            # Skip system fields and parent linking fields
            if df.fieldname in ['name', 'owner', 'creation', 'modified', 'modified_by', 'docstatus', 'parent', 'parenttype', 'parentfield', 'idx']:
                continue
            
            # Check if field is structurally required OR business-critical
            is_required = df.reqd and not df.hidden and not df.read_only
            
            # Add business-critical fields for specific child doctypes
            if child_doctype == "Purchase Order Item" and df.fieldname in ["rate", "warehouse"]:
                is_required = True
            elif child_doctype == "Sales Order Item" and df.fieldname in ["rate", "warehouse", "delivery_date"]:
                is_required = True
            
            if is_required:
                required_fields.append({
                    "fieldname": df.fieldname,
                    "label": df.label or df.fieldname.replace("_", " ").title(),
                    "fieldtype": df.fieldtype,
                    "options": df.options
                })
            elif not df.hidden and not df.read_only and df.fieldtype not in ['Section Break', 'Column Break', 'HTML']:
                optional_fields.append({
                    "fieldname": df.fieldname,
                    "label": df.label or df.fieldname.replace("_", " ").title(),
                    "fieldtype": df.fieldtype,
                    "options": df.options
                })
        
        return required_fields, optional_fields
    except Exception as e:
        frappe.log_error(f"Error getting child table fields for {child_doctype}: {str(e)}", "Child Table Error")
        return [], []

def show_child_table_collection(doctype, child_table_field, data, missing_child_tables, user):
    """Show interface to start collecting child table rows"""
    try:
        # Debug: Log the function call
        try:
            frappe.log_error(f"show_child_table_collection called: {doctype}, {child_table_field}", "Child Table Setup")
        except:
            pass
        # Get child table metadata
        meta = frappe.get_meta(doctype)
        child_table_def = meta.get_field(child_table_field)
        
        if not child_table_def:
            return f"Error: Could not find child table '{child_table_field}' in {doctype}"
        
        child_doctype = child_table_def.options
        child_table_label = child_table_def.label or child_table_field.replace("_", " ").title()
        
        # Get required and optional fields for the child table
        required_fields, optional_fields = get_child_table_fields(child_doctype)
        
        # Create beautiful interface
        response_parts = [
            f"📋 **Add {child_table_label} to {doctype}**\n",
            f"**Child Table:** {child_table_label} ({child_doctype})\n"
        ]
        
        if required_fields:
            response_parts.append("**📝 Required Fields:**")
            for field in required_fields:
                field_icon = get_field_icon(field["fieldtype"])
                response_parts.append(f"  {field_icon} **{field['label']}** ({field['fieldtype']})")
        
        if optional_fields:
            response_parts.append("\n**📄 Optional Fields:**")
            for field in optional_fields[:5]:  # Show first 5 optional fields
                field_icon = get_field_icon(field["fieldtype"])
                response_parts.append(f"  {field_icon} {field['label']} ({field['fieldtype']})")
            
            if len(optional_fields) > 5:
                response_parts.append(f"  ... and {len(optional_fields) - 5} more optional fields")
        
        response_parts.extend([
            f"\n**🎯 Let's collect the first row of {child_table_label}:**",
            "",
            "**💡 How it works:**",
            f"• I'll ask for each required field one by one",
            f"• You can add multiple rows to the {child_table_label}",
            f"• Type `skip` to skip optional fields",
            f"• Type `cancel` to cancel {doctype} creation",
            "",
            "**🚀 Ready to start? Type `yes` to begin adding the first row.**"
        ])
        
        # Save state for child table collection
        state = {
            "action": "collect_child_table",
            "doctype": doctype,
            "child_table_field": child_table_field,
            "child_doctype": child_doctype,
            "child_table_label": child_table_label,
            "data": data,
            "missing_child_tables": missing_child_tables,
            "required_fields": required_fields,
            "optional_fields": optional_fields,
            "current_row": {},
            "collected_rows": [],
            "current_field_index": 0,
            "stage": "confirm_start"
        }
        set_conversation_state(user, state)
        
        return "\n".join(response_parts)
        
    except Exception as e:
        # Enhanced error logging for child table collection
        import traceback
        try:
            frappe.log_error(f"Error in show_child_table_collection: {str(e)}", "Child Table Error") 
            frappe.log_error(traceback.format_exc(), "Child Table Traceback")
        except:
            pass
        return f"Error showing child table collection for {child_table_field}: {str(e)}"

def get_field_icon(fieldtype):
    """Get appropriate emoji icon for field type"""
    icons = {
        "Data": "✏️", "Text": "📝", "Long Text": "📄", "Small Text": "📝",
        "Link": "🔗", "Select": "📋", "Check": "☑️", 
        "Int": "🔢", "Float": "💯", "Currency": "💰", "Percent": "📊",
        "Date": "📅", "Datetime": "🕐", "Time": "⏰",
        "Text Editor": "📝", "Code": "💻", "HTML Editor": "🌐",
        "Attach": "📎", "Attach Image": "🖼️",
        "Table": "📋", "Dynamic Link": "🔗"
    }
    return icons.get(fieldtype, "📝")

def handle_child_table_collection(message, state, user):
    """Handle child table row collection conversation"""
    try:
        stage = state.get("stage", "confirm_start")
        user_input = message.strip()
        
        # Handle cancel at any stage
        if user_input.lower() in ['cancel', 'quit', 'exit']:
            clear_conversation_state(user)
            doctype = state.get("doctype", "Document")
            return f"{doctype} creation cancelled."
        
        if stage == "confirm_start":
            # User confirmation to start child table collection
            if user_input.lower() in ['yes', 'y', 'start', 'begin', '1']:
                # Start collecting the first field
                return start_child_field_collection(state, user)
            else:
                # Skip this child table
                return skip_current_child_table(state, user)
        
        elif stage == "collect_field":
            # Collecting individual field values
            return handle_child_field_input(user_input, state, user)
        
        elif stage == "add_more_rows":
            # Ask if user wants to add more rows
            if user_input.lower() in ['yes', 'y', 'add', 'more', '1']:
                # Reset for new row
                state["current_row"] = {}
                state["current_field_index"] = 0
                state["stage"] = "collect_field"
                set_conversation_state(user, state)
                return start_child_field_collection(state, user)
            else:
                # Done with this child table, move to next or create document
                return finalize_child_table_collection(state, user)
        
        else:
            clear_conversation_state(user)
            return "Error in child table collection process. Please start over."
            
    except Exception as e:
        clear_conversation_state(user)
        return f"Error processing child table collection: {str(e)}"

def start_child_field_collection(state, user):
    """Start collecting fields for a child table row"""
    try:
        required_fields = state.get("required_fields", [])
        current_field_index = state.get("current_field_index", 0)
        
        if current_field_index < len(required_fields):
            # Still have required fields to collect
            current_field = required_fields[current_field_index]
            field_name = current_field["fieldname"]
            field_label = current_field["label"]
            fieldtype = current_field["fieldtype"]
            options = current_field.get("options", "")
            
            # Create a context for child table field collection
            child_table_label = state.get("child_table_label", "Child Table")
            row_number = len(state.get("collected_rows", [])) + 1
            doctype = state.get("doctype", "")
            
            # Prepare state for smart field selection
            child_table_state = {
                "action": "collect_child_table_field",
                "doctype": doctype,
                "child_table_data": state,  # Pass the entire child table state
                "field_info": current_field,
                "data": {},  # Empty for child table context
                "missing_fields": [],  # Not used for child table
                "numbered_options": []
            }
            
            # Use smart field selection for better UX
            if fieldtype == "Link" and options:
                # Show numbered options for Link fields
                return show_child_table_link_selection(field_name, field_label, options, child_table_state, user, child_table_label, row_number)
            elif fieldtype == "Select" and options:
                # Show numbered options for Select fields
                return show_child_table_select_selection(field_name, field_label, options, child_table_state, user, child_table_label, row_number)
            elif fieldtype == "Date":
                # Show quick date options
                return show_child_table_date_selection(field_name, field_label, child_table_state, user, child_table_label, row_number)
            elif fieldtype in ["Currency", "Float", "Int", "Percent"]:
                # Show numeric input interface
                return show_child_table_numeric_input(field_name, field_label, fieldtype, child_table_state, user, child_table_label, row_number)
            else:
                # Default text input with enhanced formatting
                return show_child_table_text_input(field_name, field_label, fieldtype, child_table_state, user, child_table_label, row_number)
        else:
            # All required fields collected, finalize this row
            return finalize_current_row(state, user)
    
    except Exception as e:
        return f"Error starting field collection: {str(e)}"

def show_child_table_link_selection(field_name, field_label, link_doctype, state, user, child_table_label, row_number):
    """Show numbered options for Link fields in child tables with simple text interface"""
    try:
        # Get available records for the link doctype
        records = frappe.get_all(link_doctype, 
                                fields=["name"],
                                order_by="name",
                                limit=20)  # Limit for child table context
        
        # Try to get a better display field
        link_meta = frappe.get_meta(link_doctype)
        display_field = None
        for field in ["title", "full_name", "item_name", "uom_name", "currency_name"]:
            if link_meta.get_field(field):
                display_field = field
                break
        
        if display_field:
            records = frappe.get_all(link_doctype, 
                                   fields=["name", display_field],
                                   order_by="name",
                                   limit=20)
        
        # Create appropriate icon based on doctype
        icons = {
            "Item": "📦", "UOM": "📏", "Currency": "💱", "Customer": "👤", 
            "Supplier": "🏭", "Warehouse": "🏪", "Company": "🏢",
            "Project": "📋", "Cost Center": "🏦", "Employee": "👨‍💼"
        }
        icon = icons.get(link_doctype, "🔗")
        
        record_names = []
        
        if records:
            record_names = [record.name for record in records]
            
            # Unicode circled numbers for beautiful badges (purple theme)
            circled_numbers = ["①", "②", "③", "④", "⑤", "⑥", "⑦", "⑧", "⑨", "⑩", "⑪", "⑫", "⑬", "⑭", "⑮", "⑯", "⑰", "⑱", "⑲", "⑳"]
            
            # Create beautiful response with heavy markdown styling
            response_parts = [
                f"{icon} **{child_table_label} Row {row_number} - {field_label}**",
                f"*Choose from {len(records)} available {link_doctype.lower()}s*\n"
            ]
            
            # Add the beautiful option cards with circled numbers
            response_parts.append(f"**📋 Available {link_doctype}s:**")
            for i, record in enumerate(records, 1):
                display_name = record.name
                if display_field and record.get(display_field) and record.get(display_field) != record.name:
                    display_name += f" *({record.get(display_field)})*"
                badge = circled_numbers[i-1] if i <= len(circled_numbers) else f"({i})"
                response_parts.append(f"{badge} **{display_name}**")
            
            response_parts.extend([
                "",
                "**💡 How to select:**",
                "• Type a **number** (e.g., `3`) for your choice",
                "• Type the **{} name** directly".format(link_doctype.lower()),
                "• Type `cancel` to cancel operation",
                "",
                "**📝 Quick Examples:**",
                f"• `1` → Select **{records[0].name}**" if records else "",
                f"• `{records[0].name}` → Select by exact name" if records else "",
                "• `cancel` → Cancel this operation",
                "",
                f"**🎯 Row {row_number} {link_doctype} Selection:**",
                f"• **Field:** {field_label}",
                f"• **Row:** {row_number} in {child_table_label}",
                f"• **Available:** {len(records)} {link_doctype.lower()}s",
                f"• **Search:** Type any name for direct selection"
            ])
            
            options_text = "\n".join(response_parts)
        else:
            options_text = f"{icon} **{child_table_label} Row {row_number} - {field_label}**\n\nℹ️ No {link_doctype.lower()}s found.\n\n• Type a **name** directly\n• Type `cancel` to cancel"
        
        # Save state for child table field collection
        state["numbered_options"] = record_names
        set_conversation_state(user, state)
        
        return options_text
        
    except Exception as e:
        return f"Error showing {field_label} selection: {str(e)}"

def show_child_table_select_selection(field_name, field_label, options, state, user, child_table_label, row_number):
    """Show numbered options for Select fields in child tables with simple text formatting"""
    try:
        # Parse options (they come as newline-separated string)
        option_list = [opt.strip() for opt in options.split('\n') if opt.strip()]
        
        if option_list:
            # Unicode circled numbers for beautiful badges (purple theme)
            circled_numbers = ["①", "②", "③", "④", "⑤", "⑥", "⑦", "⑧", "⑨", "⑩", "⑪", "⑫", "⑬", "⑭", "⑮", "⑯", "⑰", "⑱", "⑲", "⑳"]
            
            # Create beautiful response with heavy markdown styling
            response_parts = [
                f"⚙️ **{child_table_label} Row {row_number} - {field_label}**",
                f"*Choose from {len(option_list)} available options*\n"
            ]
            
            # Add the beautiful option cards with circled numbers
            response_parts.append("**⚙️ Available Options:**")
            for i, option in enumerate(option_list, 1):
                badge = circled_numbers[i-1] if i <= len(circled_numbers) else f"({i})"
                response_parts.append(f"{badge} **{option}**")
            
            response_parts.extend([
                "",
                "**💡 How to select:**",
                "• Type a **number** (e.g., `3`) for your choice",
                "• Type the **option name** directly",
                "• Type `cancel` to cancel operation",
                "",
                "**📝 Quick Examples:**",
                f"• `1` → Select **{option_list[0]}**" if option_list else "",
                f"• `{option_list[0]}` → Select by exact name" if option_list else "",
                "• `cancel` → Cancel this operation",
                "",
                f"**🎯 Row {row_number} Option Selection:**",
                f"• **Field:** {field_label}",
                f"• **Row:** {row_number} in {child_table_label}",
                f"• **Options:** {len(option_list)} available",
                f"• **Type:** Select (Dropdown)"
            ])
            
            options_text = "\n".join(response_parts)
        else:
            options_text = f"⚙️ **{child_table_label} Row {row_number} - {field_label}**\n\nℹ️ No options available.\n\n• Type `cancel` to cancel\n• Contact administrator to configure options"
        
        # Save state for child table field collection
        state["numbered_options"] = option_list
        set_conversation_state(user, state)
        
        return options_text
        
    except Exception as e:
        return f"Error showing {field_label} selection: {str(e)}"

def show_child_table_date_selection(field_name, field_label, state, user, child_table_label, row_number):
    """Show simple date picker for Date fields in child tables"""
    try:
        from datetime import date, timedelta
        
        today = date.today()
        
        # For delivery_date, ensure all options are today or future
        if field_name == "delivery_date":
            option1 = today
            option2 = today + timedelta(days=1)
            option3 = today + timedelta(days=7)
            option4 = today + timedelta(days=30)
            
            option1_label = "Today"
            option2_label = "Tomorrow"
            option3_label = "Next Week"
            option4_label = "Next Month"
        else:
            # For other date fields, use normal options
            option1 = today
            option2 = today + timedelta(days=1)
            option3 = today + timedelta(days=7)
            option4 = today + timedelta(days=30)
            
            option1_label = "Today"
            option2_label = "Tomorrow"
            option3_label = "Next Week"
            option4_label = "Next Month"
        
        date_options = [
            option1.strftime("%Y-%m-%d"),
            option2.strftime("%Y-%m-%d"), 
            option3.strftime("%Y-%m-%d"),
            option4.strftime("%Y-%m-%d")
        ]
        
        # Get current year for examples
        current_year = today.year
        future_date1 = f"{current_year}-12-25"
        future_date2 = f"{current_year + 1}-06-15"
        future_date3 = f"{current_year + 1}-03-01"
        
        # Unicode circled numbers for beautiful badges (purple theme)
        circled_numbers = ["①", "②", "③", "④", "⑤", "⑥", "⑦", "⑧", "⑨", "⑩"]
        
        # Create beautiful response with heavy markdown styling
        response_parts = [
            f"📅 **{child_table_label} Row {row_number} - {field_label}**",
            f"*Choose a date for your {field_label.lower()}*\n"
        ]
        
        # Add beautiful quick date options with circled numbers
        response_parts.extend([
            "**⚡ Quick Date Options:**",
            f"{circled_numbers[0]} **{option1_label}** - `{option1.strftime('%Y-%m-%d')}` ({option1.strftime('%A')})",
            f"{circled_numbers[1]} **{option2_label}** - `{option2.strftime('%Y-%m-%d')}` ({option2.strftime('%A')})",
            f"{circled_numbers[2]} **{option3_label}** - `{option3.strftime('%Y-%m-%d')}` ({option3.strftime('%A')})",
            f"{circled_numbers[3]} **{option4_label}** - `{option4.strftime('%Y-%m-%d')}` ({option4.strftime('%A')})",
            ""
        ])
        
        response_parts.extend([
            "**💡 How to select:**",
            "• Type a **number** (e.g., `2`) for quick date options",
            "• Type a **custom date** in `YYYY-MM-DD` format",
            "• Type `cancel` to cancel operation"
        ])
        
        # Add specific instructions for delivery date
        if field_name == "delivery_date":
            response_parts.extend([
                "",
                "**⚠️ Important Note:**",
                "• **Delivery date must be today or later**",
                "• Past dates will be rejected automatically"
            ])
        
        response_parts.extend([
            "",
            "**📝 Custom Date Examples:**",
            f"• `{future_date1}` → Christmas {current_year}",
            f"• `{future_date2}` → Mid-year {current_year + 1}",
            f"• `{future_date3}` → March 1st {current_year + 1}",
            "",
            f"**🎯 Row {row_number} Date Selection:**",
            f"• **Field:** {field_label}",
            f"• **Row:** {row_number} in {child_table_label}",
            f"• **Today's Date:** {today.strftime('%Y-%m-%d')} ({today.strftime('%A')})",
            f"• **Format Required:** YYYY-MM-DD"
        ])
        
        # Save state for child table field collection
        state["numbered_options"] = date_options
        set_conversation_state(user, state)
        
        return "\n".join(response_parts)
        
    except Exception as e:
        return f"Error showing {field_label} selection: {str(e)}"

def show_child_table_numeric_input(field_name, field_label, fieldtype, state, user, child_table_label, row_number):
    """Show simple numeric input interface for numeric fields in child tables"""
    try:
        # Create appropriate icon and examples based on field type
        if fieldtype == "Int":
            icon = "🔢"
            examples = ["100", "250", "1000"]
            description = f"whole number for {field_label.lower()}"
            format_info = ["Positive numbers: 100, 250", "Zero: 0", "No negative values allowed"]
        elif fieldtype == "Currency":
            icon = "💰"
            examples = ["100.50", "25000", "1000.99"]
            description = f"amount for {field_label.lower()}"
            format_info = ["Decimals: 100.50, 25000.75", "Whole amounts: 100, 250", "Large amounts: 1000000"]
        elif fieldtype == "Percent":
            icon = "📊" 
            examples = ["15", "25.5", "100"]
            description = f"percentage for {field_label.lower()}"
            format_info = ["Whole percent: 15, 50", "Decimal percent: 25.5, 12.75", "Range: 0 to 100"]
        else:  # Float
            icon = "💯"
            examples = ["100.50", "25.75", "1000.99"]
            description = f"decimal number for {field_label.lower()}"
            format_info = ["Decimals: 100.50, 25.75", "Whole numbers: 100, 250", "Scientific: 1e3 (1000)"]
        
        # Create simple text-based interface
        response_parts = [
            f"{icon} **{child_table_label} Row {row_number} - {field_label}**\n",
            f"Enter a {description}\n",
            "**💡 How to enter:**",
            f"• Type a {description}",
            "• Type `0` if no value",
            "• Type `cancel` to cancel\n",
            f"**📝 Examples:** `{examples[0]}`, `{examples[1]}`, `{examples[2]}`\n",
            "**ℹ️ Supported formats:**"
        ]
        
        for format_item in format_info:
            response_parts.append(f"• {format_item}")
        
        # Save state for child table field collection
        state["numbered_options"] = []
        set_conversation_state(user, state)
        
        return "\n".join(response_parts)
        
    except Exception as e:
        return f"Error showing {field_label} input: {str(e)}"

def show_child_table_text_input(field_name, field_label, fieldtype, state, user, child_table_label, row_number):
    """Show simple text input interface for text fields in child tables"""
    try:
        # Get appropriate icon based on field type
        field_icons = {
            "Data": "✏️",
            "Text": "📝", 
            "Small Text": "📝",
            "Text Editor": "📄"
        }
        
        icon = field_icons.get(fieldtype, "✏️")
        
        # Create context-specific examples
        if "name" in field_name.lower():
            example = f"Your {field_label.lower()} here"
        elif "description" in field_name.lower():
            example = f"Description of the {field_label.lower()}"
        elif "code" in field_name.lower():
            example = f"CODE123"
        else:
            example = f"Your {field_label.lower()} here"
        
        # Create simple text-based interface
        response_parts = [
            f"{icon} **{child_table_label} Row {row_number} - {field_label}**\n",
            "**💡 How to enter:**",
            "• Type your text directly",
            "• Type `cancel` to cancel",
            "",
            f"**📝 Example:** `{example}`"
        ]
        
        options_text = "\n".join(response_parts)
        
        # Save state for child table field collection
        state["numbered_options"] = []
        set_conversation_state(user, state)
        
        return options_text
        
    except Exception as e:
        return f"Error showing {field_label} input: {str(e)}"

def handle_child_table_field_input(message, state, user):
    """Handle input for child table fields with enhanced numbered options"""
    try:
        user_input = message.strip()
        
        # Handle cancel at any stage
        if user_input.lower() in ['cancel', 'quit', 'exit']:
            clear_conversation_state(user)
            doctype = state.get("doctype", "Document")
            return f"{doctype} creation cancelled."
        
        # Get the child table state and field info
        child_table_data = state.get("child_table_data", {})
        field_info = state.get("field_info", {})
        numbered_options = state.get("numbered_options", [])
        
        fieldname = field_info["fieldname"]
        fieldtype = field_info["fieldtype"]
        field_label = field_info["label"]
        
        selected_value = None
        
        # Handle numbered options first (for Link, Select, Date fields)
        if numbered_options and user_input.isdigit():
                try:
                    num = int(user_input)
                    if 1 <= num <= len(numbered_options):
                      selected_value = numbered_options[num - 1]
                    else:
                        return f"❌ Invalid number: {num}. Please use numbers between 1 and {len(numbered_options)}."
                except ValueError:
                        return f"❌ Invalid input. Please use numbers or direct input."
        else:
            # Handle direct input or non-numeric fields
            try:
                # Use the existing validation function
                selected_value = validate_field_input(user_input, field_info)
            except ValueError as e:
                # Show error and ask again with appropriate interface
                return f"❌ {str(e)}\n\nPlease try again or type `cancel` to cancel."
        
        # If we got a valid value, update the child table data
        if selected_value is not None:
            current_row = child_table_data.get("current_row", {})
            current_row[fieldname] = selected_value
            
            # Move to next field
            current_field_index = child_table_data.get("current_field_index", 0)
            child_table_data["current_row"] = current_row
            child_table_data["current_field_index"] = current_field_index + 1
            
            # Update the original child table state
            state["child_table_data"] = child_table_data
            
            # Continue with the original child table collection flow
            child_table_data["stage"] = "collect_field"
            set_conversation_state(user, child_table_data)
            
            return start_child_field_collection(child_table_data, user)
        else:
            return "❌ Invalid input. Please try again or type `cancel` to cancel."
            
    except Exception as e:
        clear_conversation_state(user)
        return f"Error processing child table field input: {str(e)}"

def handle_child_field_input(user_input, state, user):
    """Handle input for a specific child table field"""
    try:
        required_fields = state.get("required_fields", [])
        current_field_index = state.get("current_field_index", 0)
        current_row = state.get("current_row", {})
        
        if current_field_index >= len(required_fields):
            return finalize_current_row(state, user)
        
        current_field = required_fields[current_field_index]
        fieldname = current_field["fieldname"]
        fieldtype = current_field["fieldtype"]
        
        # Validate and convert the input based on field type
        try:
            validated_value = validate_field_input(user_input, current_field)
            current_row[fieldname] = validated_value
            
            # Move to next field
            state["current_row"] = current_row
            state["current_field_index"] = current_field_index + 1
            set_conversation_state(user, state)
            
            return start_child_field_collection(state, user)
            
        except ValueError as e:
            # Invalid input, ask again
            field_icon = get_field_icon(fieldtype)
            return f"❌ {str(e)}\n\n{field_icon} **Please enter {current_field['label']} again:**\n{get_field_input_help(current_field)}"
    
    except Exception as e:
        return f"Error handling field input: {str(e)}"

def validate_field_input(user_input, field_info):
    """Validate user input based on field type"""
    fieldtype = field_info["fieldtype"]
    fieldname = field_info["fieldname"]
    
    if fieldtype == "Int":
        try:
            return int(float(user_input))
        except ValueError:
            raise ValueError(f"Invalid integer value. Please enter a whole number.")
    
    elif fieldtype in ["Float", "Currency", "Percent"]:
        try:
            return float(user_input)
        except ValueError:
            raise ValueError(f"Invalid number. Please enter a decimal number.")
    
    elif fieldtype == "Date":
        import re
        if re.match(r'^\d{4}-\d{2}-\d{2}$', user_input):
            from datetime import datetime, date
            try:
                input_date = datetime.strptime(user_input, '%Y-%m-%d').date()
                
                # Special validation for delivery_date - must be today or future
                if fieldname == "delivery_date":
                    today = date.today()
                    if input_date < today:
                        raise ValueError(f"Delivery date cannot be in the past. Please enter a date from {today.strftime('%Y-%m-%d')} onwards.")
                
                return user_input
            except ValueError as e:
                if "Delivery date cannot be in the past" in str(e):
                    raise e
                else:
                    raise ValueError("Invalid date. Please use YYYY-MM-DD format.")
        else:
            raise ValueError("Invalid date format. Please use YYYY-MM-DD format (e.g., 2024-12-31).")
    
    elif fieldtype == "Link":
        # Check if linked document exists
        link_doctype = field_info.get("options")
        if link_doctype and not frappe.db.exists(link_doctype, user_input):
            raise ValueError(f"{link_doctype} '{user_input}' does not exist. Please use an existing {link_doctype}.")
        return user_input
    
    elif fieldtype == "Select":
        # Validate against available options
        options = field_info.get("options", "")
        if options:
            valid_options = [opt.strip() for opt in options.split('\n') if opt.strip()]
            if user_input not in valid_options:
                options_text = ", ".join(valid_options)
                raise ValueError(f"Invalid option. Please choose from: {options_text}")
        return user_input
    
    else:
        # For Data, Text, etc. - basic validation
        if not user_input.strip():
            raise ValueError("This field cannot be empty. Please enter a value.")
        return user_input.strip()

def get_field_input_help(field_info):
    """Get help text for field input based on field type"""
    fieldtype = field_info["fieldtype"]
    
    if fieldtype == "Int":
        return "**Example:** `100`, `250`, `1000`"
    elif fieldtype in ["Float", "Currency"]:
        return "**Example:** `100.50`, `25.75`, `1000.99`"
    elif fieldtype == "Percent":
        return "**Example:** `15`, `25.5`, `100`"
    elif fieldtype == "Date":
        return "**Example:** `2024-12-31`, `2024-06-15`\n**Format:** YYYY-MM-DD"
    elif fieldtype == "Link":
        link_doctype = field_info.get("options", "")
        return f"**Example:** Enter an existing {link_doctype} name"
    elif fieldtype == "Select":
        options = field_info.get("options", "")
        if options:
            valid_options = [opt.strip() for opt in options.split('\n') if opt.strip()][:3]
            return f"**Options:** {', '.join(valid_options)}"
        return "**Example:** Select from available options"
    else:
        return "**Example:** Enter text value"

def finalize_current_row(state, user):
    """Finalize the current child table row and ask if user wants to add more"""
    try:
        current_row = state.get("current_row", {})
        collected_rows = state.get("collected_rows", [])
        child_table_label = state.get("child_table_label", "Child Table")
        
        # Add current row to collected rows
        collected_rows.append(current_row.copy())
        
        # Show summary of added row
        response_parts = [
            "✅ **Row Added Successfully!**\n",
            f"**{child_table_label} Row {len(collected_rows)}:**"
        ]
        
        for fieldname, value in current_row.items():
            # Get field label from required_fields
            field_label = fieldname
            for field in state.get("required_fields", []):
                if field["fieldname"] == fieldname:
                    field_label = field["label"]
                    break
            response_parts.append(f"• **{field_label}:** {value}")
        
        response_parts.extend([
            f"\n📋 **Total {child_table_label} Rows:** {len(collected_rows)}",
            "",
            "🔄 **Add Another Row?**",
            f"• Type `yes` to add another {child_table_label} row",
            f"• Type `no` to continue with document creation"
        ])
        
        # Update state
        state.update({
            "stage": "add_more_rows",
            "collected_rows": collected_rows,
            "current_row": {},
            "current_field_index": 0
        })
        set_conversation_state(user, state)
        
        return "\n".join(response_parts)
        
    except Exception as e:
        return f"Error finalizing row: {str(e)}"

def finalize_child_table_collection(state, user):
    """Finalize child table collection and continue with document creation"""
    try:
        doctype = state.get("doctype")
        child_table_field = state.get("child_table_field")
        collected_rows = state.get("collected_rows", [])
        data = state.get("data", {})
        missing_child_tables = state.get("missing_child_tables", [])
        
        # Add collected rows to document data
        data[child_table_field] = collected_rows
        
        # Remove this child table from missing list
        if child_table_field in missing_child_tables:
            missing_child_tables.remove(child_table_field)
        
        # Check if we have more child tables to collect
        if missing_child_tables:
            # Move to next child table
            next_child_table = missing_child_tables[0]
            return show_child_table_collection(doctype, next_child_table, data, missing_child_tables, user)
        else:
            # All child tables collected, create the document
            clear_conversation_state(user)
            return create_document(doctype, data, user)
            
    except Exception as e:
        clear_conversation_state(user)
        return f"Error finalizing child table collection: {str(e)}"

def skip_current_child_table(state, user):
    """Skip the current child table and move to next or create document"""
    try:
        doctype = state.get("doctype")
        child_table_field = state.get("child_table_field")
        data = state.get("data", {})
        missing_child_tables = state.get("missing_child_tables", [])
        
        # Remove this child table from missing list (skip it)
        if child_table_field in missing_child_tables:
            missing_child_tables.remove(child_table_field)
        
        # Check if we have more child tables to collect
        if missing_child_tables:
            # Move to next child table
            next_child_table = missing_child_tables[0]
            return show_child_table_collection(doctype, next_child_table, data, missing_child_tables, user)
        else:
            # No more child tables, create the document
            clear_conversation_state(user)
            return create_document(doctype, data, user)
            
    except Exception as e:
        clear_conversation_state(user)
        return f"Error skipping child table: {str(e)}"

def get_optional_child_tables(doctype):
    """Get list of optional but commonly used child tables for a doctype"""
    try:
        meta = frappe.get_meta(doctype)
        optional_child_tables = []
        
        for df in meta.fields:
            if df.fieldtype == "Table" and not df.reqd:
                # Include commonly useful optional child tables
                if df.fieldname in ['contacts', 'addresses', 'payment_schedule', 'education', 'external_work_history', 'item_prices']:
                    optional_child_tables.append({
                        "fieldname": df.fieldname,
                        "label": df.label or df.fieldname.replace("_", " ").title(),
                        "options": df.options
                    })
        
        return optional_child_tables
    except Exception as e:
        frappe.log_error(f"Error getting optional child tables for {doctype}: {str(e)}", "Child Table Error")
        return []

def suggest_optional_child_tables(doctype, data, user):
    """Suggest optional child tables that user might want to add"""
    try:
        optional_child_tables = get_optional_child_tables(doctype)
        
        if not optional_child_tables:
            return None
        
        response_parts = [
            f"✅ **{doctype} Created Successfully!**\n",
            "**📋 Optional Child Tables Available:**",
            "_You can add these for more complete data:_\n"
        ]
        
        for i, child_table in enumerate(optional_child_tables, 1):
            response_parts.append(f"`{i}` **{child_table['label']}** ({child_table['options']})")
        
        response_parts.extend([
            "",
            "**💡 Would you like to add any of these?**",
            "• Type a **number** to add that child table",
            "• Type `no` or `done` to finish",
            "• All child tables can be added later via ERPNext UI"
        ])
        
        return "\n".join(response_parts)
        
    except Exception as e:
        return None

@frappe.whitelist()
def test_child_table_detection(doctype):
    """Test function to see what child tables are detected for a doctype"""
    try:
        if not frappe.has_permission(doctype, "read"):
            return {"error": f"No permission to access {doctype}"}
        
        required_child_tables = get_required_child_tables(doctype)
        optional_child_tables = get_optional_child_tables(doctype)
        
        # Get all child tables for comparison
        meta = frappe.get_meta(doctype)
        all_child_tables = []
        for df in meta.fields:
            if df.fieldtype == "Table":
                all_child_tables.append({
                    "fieldname": df.fieldname,
                    "label": df.label or df.fieldname.replace("_", " ").title(),
                    "options": df.options,
                    "required": df.reqd
                })
        
        return {
            "doctype": doctype,
            "required_child_tables": required_child_tables,
            "optional_child_tables": optional_child_tables,
            "all_child_tables": all_child_tables,
            "total_child_tables": len(all_child_tables)
        }
        
    except Exception as e:
        return {"error": str(e)}

@frappe.whitelist()
def test_child_table_fields(child_doctype):
    """Test function to see what fields are detected for a child doctype"""
    try:
        if not frappe.db.exists("DocType", child_doctype):
            return {"error": f"DocType {child_doctype} does not exist"}
        
        required_fields, optional_fields = get_child_table_fields(child_doctype)
        
        return {
            "child_doctype": child_doctype,
            "required_fields": required_fields,
            "optional_fields": optional_fields,
            "total_required": len(required_fields),
            "total_optional": len(optional_fields)
        }
        
    except Exception as e:
        return {"error": str(e)}

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
            
            # Stock Entry specific hardcoded logic removed - now handled by generic system
            
            # Special handling for Asset fields with interactive selection
            if doctype == "Asset":
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
            
            # Hardcoded Stock Entry check removed - now handled by generic child table system
            
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

def handle_pagination_navigation(state, new_page, user):
    """Handle navigation to a different page in paginated selections"""
    try:
        selection_type = state.get("selection_type")
        data = state.get("data", {})
        missing_fields = state.get("missing_fields", [])
        current_doctype = state.get("doctype")
        
        # Determine which paginated selection to show
        if selection_type == "currency":
            field_label = "Currency"
            return show_currency_link_selection(selection_type, field_label, data, missing_fields, user, current_doctype, new_page)
        elif selection_type in ["customer", "supplier", "item_code", "employee"]:
            # Use paginated generic link selection for these
            field_label = selection_type.replace("_", " ").title()
            link_doctype = {
                "customer": "Customer",
                "supplier": "Supplier", 
                "item_code": "Item",
                "employee": "Employee"
            }.get(selection_type, selection_type)
            return show_paginated_link_selection(selection_type, field_label, link_doctype, data, missing_fields, user, current_doctype, new_page)
        else:
            # Fallback - show same page again
            return "❌ Pagination not supported for this field type."
            
    except Exception as e:
        return f"Error handling pagination: {str(e)}"

def handle_stock_selection_collection(message, state, user):
    """Handle collection of stock entry field selections"""
    try:
        selection_type = state.get("selection_type")
        data = state.get("data")
        missing_fields = state.get("missing_fields")
        numbered_options = state.get("numbered_options", [])
        user_input = message.strip()
        
        # Debug: Log the function entry state (truncated to avoid "Value too big" error)
        try:
            field_count = len(missing_fields) if missing_fields else 0
            frappe.log_error(f"Function entry: field_count={field_count}", "Function Entry Debug")
        except:
            pass
        
        # Handle cancel
        if user_input.lower() in ['cancel', 'quit', 'exit']:
            clear_conversation_state(user)
            doctype_name = state.get("doctype", "Document")
            return f"{doctype_name} creation cancelled."
        
        # Handle pagination navigation
        pagination = state.get("pagination")
        if pagination and user_input.lower() in ['next', 'next_page', 'next page']:
            current_page = pagination.get("current_page", 1)
            total_pages = pagination.get("total_pages", 1)
            if current_page < total_pages:
                # Show next page for the same field
                return handle_pagination_navigation(state, current_page + 1, user)
            else:
                return "❌ Already on the last page. Please select an option or type 'cancel'."
        
        elif pagination and user_input.lower() in ['prev', 'previous', 'prev_page', 'previous page']:
            current_page = pagination.get("current_page", 1)
            if current_page > 1:
                # Show previous page for the same field
                return handle_pagination_navigation(state, current_page - 1, user)
            else:
                return "❌ Already on the first page. Please select an option or type 'cancel'."
        
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
            # For currency fields, search across all currencies first
            all_currency_options = state.get("all_currency_options", [])
            if selection_type == "currency" and all_currency_options:
                # Search across all currencies, not just current page
                exact_matches = [opt for opt in all_currency_options if opt.lower() == user_input.lower()]
                if len(exact_matches) == 1:
                    selected_value = exact_matches[0]
                elif len(exact_matches) > 1:
                    selected_value = exact_matches[0]  # Take first exact match
                else:
                    # Try partial match across all currencies
                    matching_options = [opt for opt in all_currency_options if user_input.lower() in opt.lower()]
                    if len(matching_options) == 1:
                        selected_value = matching_options[0]
                    elif len(matching_options) > 1:
                        # Show first few matches for user to choose from
                        match_list = ", ".join(matching_options[:5])
                        return f"Multiple currencies found matching '{user_input}': {match_list}. Please be more specific."
                    else:
                        return f"Currency '{user_input}' not found. Please use numbers (e.g., 1, 2, 3) or exact currency codes like USD, INR, EUR."
            elif numbered_options:
                # Standard search for non-currency fields
                # First try exact match (case-insensitive)
                exact_matches = [opt for opt in numbered_options if opt.lower() == user_input.lower()]
                if len(exact_matches) == 1:
                    selected_value = exact_matches[0]
                elif len(exact_matches) > 1:
                    selected_value = exact_matches[0]  # Take first exact match
                else:
                    # Then try partial match
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
        
        # Get current doctype from state early - needed for Payment Entry logic
        current_doctype = state.get("doctype")
        
        # Define remaining_fields early so it can be used in Payment Entry logic
        remaining_fields = missing_fields.copy()
        
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
            
            # CRITICAL FIX: Handle Payment Entry party_type auto-setting
            if selection_type == "payment_type" and current_doctype == "Payment Entry":
                # Auto-set party_type based on payment_type
                if selected_value == "Receive":
                    data["party_type"] = "Customer"
                    # Remove party_type from remaining fields since we just set it
                    if "party_type" in remaining_fields:
                        remaining_fields.remove("party_type")
                elif selected_value == "Pay":
                    data["party_type"] = "Supplier"
                    # Remove party_type from remaining fields since we just set it
                    if "party_type" in remaining_fields:
                        remaining_fields.remove("party_type")
                elif selected_value == "Internal Transfer":
                    # For Internal Transfer, party is not required
                    if "party_type" in remaining_fields:
                        remaining_fields.remove("party_type")
                    if "party" in remaining_fields:
                        remaining_fields.remove("party")
                try:
                    frappe.log_error(f"Auto-set party_type for Payment Entry: {data.get('party_type')}", "Payment Entry Auto-Set")
                except:
                    pass
            
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
        
        # Remove the field we just populated from remaining_fields
        if selection_type and selection_type in remaining_fields:
            remaining_fields.remove(selection_type)
        elif missing_fields:
            remaining_fields.pop(0)
        
        # For warehouse selections, also remove related field names
        if selection_type == "from_warehouse" and "s_warehouse" in remaining_fields:
            remaining_fields.remove("s_warehouse")
        elif selection_type == "to_warehouse" and "t_warehouse" in remaining_fields:
            remaining_fields.remove("t_warehouse")
        
        # Debug: Log the doctype from state (data truncated to avoid char limit)
        try:
            data_summary = f"{len(data)} fields" if data else "no data"
            frappe.log_error(f"Doctype: {current_doctype}, Data: {data_summary}, Selection: {selection_type}", "State Debug")
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
                # Ready to collect items for Stock Entry - use generic child table system
                pass  # Will be handled by generic child table collection
        elif remaining_fields:
            # Continue with next field
            next_field = remaining_fields[0]
            
            # Handle Stock Entry specific fields first
            if next_field in ["stock_entry_type", "purpose", "voucher_type"]:
                return show_stock_entry_type_selection(data, remaining_fields, user)
            elif next_field in ["from_warehouse", "to_warehouse", "s_warehouse", "t_warehouse"]:
                return show_warehouse_selection(next_field, data, remaining_fields, user)
            elif next_field == "items":
                # Handle items field using generic child table system
                # This will be handled automatically by the generic field collection
                pass
            else:
                # Transaction document logic removed - now handled by generic child table system
                # Default dates will be handled by field defaults or validation
                
                # Use smart field selection for all other fields
                # CRITICAL: Don't default to Stock Entry, use current doctype from state

                
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
                
                meta = frappe.get_meta(current_doctype)
                field_obj = meta.get_field(next_field)
                
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
            
            # Debug: Log the final doctype determination (truncated data keys)
            try:
                key_count = len(data.keys()) if data else 0
                frappe.log_error(f"Final doctype: {current_doctype}, Keys: {key_count}", "Doctype Debug")
            except:
                pass
            
            # CRITICAL FIX: Check for required child tables before creating document
            try:
                frappe.log_error(f"About to call get_required_child_tables for: {current_doctype}", "Child Table Start")
                required_child_tables = get_required_child_tables(current_doctype)
                frappe.log_error(f"get_required_child_tables returned: {required_child_tables}", "Child Table Result")
            except Exception as e:
                frappe.log_error(f"Error in get_required_child_tables: {str(e)}", "Child Table Error")
                required_child_tables = []
            
            missing_child_tables = []
            
            try:
                for child_table in required_child_tables:
                    if child_table not in data or not data[child_table]:
                        missing_child_tables.append(child_table)
                frappe.log_error(f"Missing child tables calculation complete: {missing_child_tables}", "Child Table Missing")
            except Exception as e:
                frappe.log_error(f"Error calculating missing child tables: {str(e)}", "Child Table Missing Error")
            
            # Debug: Log child table transition
            try:
                frappe.log_error(f"Child check for {current_doctype}: required={required_child_tables}, missing={missing_child_tables}", "Final Child Check")
            except:
                    pass
            
            if missing_child_tables:
                # Need to collect child tables first
                child_table_to_collect = missing_child_tables[0]
                try:
                    frappe.log_error(f"Final transition to child table: {child_table_to_collect} for {current_doctype}", "Final Child Transition")
                except:
                    pass
                return show_child_table_collection(current_doctype, child_table_to_collect, data, missing_child_tables, user)
            else:
                # All required fields and child tables are present, create the document
                clear_conversation_state(user)
                return create_document(current_doctype, data, user)
            
    except Exception as e:
        # Enhanced error logging for debugging (truncated to avoid char limit)
        import traceback
        error_msg = str(e)[:80] + "..." if len(str(e)) > 80 else str(e)
        try:
            frappe.log_error(f"Error in handle_stock_selection_collection: {error_msg}", "Nexchat Error")
            # Log full traceback separately if needed
            frappe.log_error(traceback.format_exc(), "Nexchat Traceback")
        except:
            pass
        clear_conversation_state(user)
        return f"Error processing selection: {str(e)}. Please try again or contact support."

# handle_item_details_collection and related functions removed - replaced by generic child table system

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
- "create_doctype": Create a new DocType definition
- "list": Show/list documents  
- "get": Get specific document information
- "update": Update existing document fields
- "delete": Delete a document
- "assign": Assign/link documents or roles
- "help": Provide help information

CRITICAL FIELD MAPPING for Item Group:
- "parent" or "parent group" or "parent item group" -> "parent_item_group"
- "make child of" or "child of" -> set parent_item_group to target
- "is group" or "group" or "make group" -> "is_group" (use 1 for true, 0 for false)

CRITICAL NATURAL LANGUAGE PATTERNS:
- "make X child of Y" -> update X set parent_item_group to Y
- "set parent of X to Y" -> update X set parent_item_group to Y  
- "X should be under Y" -> update X set parent_item_group to Y
- "X parent to Y" -> update X set parent_item_group to Y
- "make X a group" -> update X set is_group to 1
- Handle typos: "biomasss" -> "Biomass", "raw materials" -> "Raw Material"

CRITICAL CREATE WITH RELATIONSHIP PATTERNS:
- "Add X child of Y" -> create X with parent_item_group = Y
- "Create X under Y" -> create X with parent_item_group = Y
- "Add X item group child of Y" -> create Item Group X with parent_item_group = Y
- "Create X as child of Y" -> create X with parent_item_group = Y
- "New X under Y" -> create X with parent_item_group = Y

CRITICAL DOCTYPE CREATION PATTERNS (HIGHEST PRIORITY):
- "create a doctype" -> action: "create_doctype"
- "create new doctype" -> action: "create_doctype"
- "make a doctype" -> action: "create_doctype"
- "create doctype using module X" -> action: "create_doctype" with module
- "new doctype for module X" -> action: "create_doctype" with module
- "create a doctype suing module" -> action: "create_doctype" (handle typos)

VERY IMPORTANT: If you see "doctype" in the request, it's likely asking to create a DocType definition, NOT a document instance. Always use action: "create_doctype" for these requests.

Examples:
- "Create a new customer" -> {{"doctype": "Customer", "action": "create", "data": {{}}}}
- "Create item with name Widget" -> {{"doctype": "Item", "action": "create", "data": {{"item_name": "Widget"}}}}
- "Add a biomass item group child of raw material" -> {{"doctype": "Item Group", "action": "create", "data": {{"item_group_name": "Biomass", "parent_item_group": "Raw Material"}}}}
- "Create finished goods under products" -> {{"doctype": "Item Group", "action": "create", "data": {{"item_group_name": "Finished Goods", "parent_item_group": "Products"}}}}
- "Add electronics item group child of products" -> {{"doctype": "Item Group", "action": "create", "data": {{"item_group_name": "Electronics", "parent_item_group": "Products"}}}}
- "create a doctype using module franchise onboarding" -> {{"action": "create_doctype", "module": "Franchise Onboarding"}}
- "create a doctype suing module franchise onboarding" -> {{"action": "create_doctype", "module": "Franchise Onboarding"}}
- "create a new doctype" -> {{"action": "create_doctype"}}
- "make a new doctype called Employee Skill" -> {{"action": "create_doctype", "doctype_name": "Employee Skill"}}
- "create doctype for CRM module" -> {{"action": "create_doctype", "module": "CRM"}}
- "new doctype" -> {{"action": "create_doctype"}}
- "Show me all customers" -> {{"doctype": "Customer", "action": "list", "filters": {{}}}}
- "List items where item_group is Raw Material" -> {{"doctype": "Item", "action": "list", "filters": {{"item_group": "Raw Material"}}}}
- "Get customer CUST-001" -> {{"doctype": "Customer", "action": "get", "filters": {{"name": "CUST-001"}}}}
- "Update customer CUST-001 set customer_name to ABC Corp" -> {{"doctype": "Customer", "action": "update", "filters": {{"name": "CUST-001"}}, "data": {{"customer_name": "ABC Corp"}}}}
- "Update Item Group Biomass set parent_item_group to Raw Material and is_group to true" -> {{"doctype": "Item Group", "action": "update", "filters": {{"name": "Biomass"}}, "data": {{"parent_item_group": "Raw Material", "is_group": 1}}}}
- "make biomass item group child of raw materials" -> {{"doctype": "Item Group", "action": "update", "filters": {{"name": "Biomass"}}, "data": {{"parent_item_group": "Raw Material"}}}}
- "update parent item group of biomass to Raw Material" -> {{"doctype": "Item Group", "action": "update", "filters": {{"name": "Biomass"}}, "data": {{"parent_item_group": "Raw Material"}}}}
- "biomasss parent item group to raw material" -> {{"doctype": "Item Group", "action": "update", "filters": {{"name": "Biomass"}}, "data": {{"parent_item_group": "Raw Material"}}}}
- "make biomass a group" -> {{"doctype": "Item Group", "action": "update", "filters": {{"name": "Biomass"}}, "data": {{"is_group": 1}}}}
- "Change first name of user@example.com" -> {{"doctype": "User", "action": "update", "filters": {{"name": "user@example.com"}}, "field_to_update": "first_name"}}
- "Delete sales order SO-001" -> {{"doctype": "Sales Order", "action": "delete", "filters": {{"name": "SO-001"}}}}
- "Assign Sales User role to user@example.com" -> {{"doctype": "User", "action": "assign", "target": "user@example.com", "assign_type": "role", "value": "Sales User"}}
- "Show all roles" -> {{"action": "list_roles"}}
- "Help me with sales orders" -> {{"action": "help", "topic": "Sales Order"}}

Important:
- Only use doctypes from the available list
- For create actions, include any mentioned field values in the 'data' object
- For list/get actions, use 'filters' to specify search criteria
- For update actions: Always use 'data' object with proper field names
- Always extract document identifiers into 'filters' for update/get/delete actions
- Handle typos and case variations in document names (e.g., "biomasss" -> "Biomass", "raw materials" -> "Raw Material")
- Convert boolean expressions: "true"/"yes"/"1" -> 1, "false"/"no"/"0" -> 0
- Map natural language field references to actual ERPNext field names
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

def execute_task(task_json, user, user_input=""):
    """Execute the task based on the parsed JSON from Gemini"""
    
    action = task_json.get("action")
    doctype = task_json.get("doctype")
    
    # Fix for common doctype name issues (spaces missing)
    doctype_mappings = {
        "ItemGroup": "Item Group",
        "CustomerGroup": "Customer Group", 
        "SupplierGroup": "Supplier Group",
        "SalesOrder": "Sales Order",
        "PurchaseOrder": "Purchase Order",
        "SalesInvoice": "Sales Invoice",
        "PurchaseInvoice": "Purchase Invoice",
        "StockEntry": "Stock Entry",
        "CostCenter": "Cost Center",
        "WarehouseType": "Warehouse Type"
    }
    
    # Check if we need to map the doctype name
    if doctype and doctype in doctype_mappings:
        original_doctype = doctype
        doctype = doctype_mappings[doctype]
        task_json["doctype"] = doctype  # Update the task_json as well
        try:
            frappe.log_error(f"Mapped doctype '{original_doctype}' to '{doctype}'", "Doctype Mapping")
        except:
            pass
    
    # CRITICAL DEBUG: Log what Gemini detected (truncated to avoid char limit)
    try:
        json_summary = f"{len(task_json)} keys" if task_json else "empty"
        full_json = str(task_json)[:200] + "..." if len(str(task_json)) > 200 else str(task_json)
        frappe.log_error(f"Gemini - Action: {action}, Doctype: {doctype}, JSON: {json_summary}, Full: {full_json}", "Gemini Debug")
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
        # Check for doctype creation patterns as fallback
        user_input_lower = user_input.lower()
        if any(pattern in user_input_lower for pattern in ['create doctype', 'create a doctype', 'new doctype', 'make doctype']):
            return handle_create_doctype_action(task_json, user, user_input)
        
        # Create beautiful help message for unclear requests
        response_parts = [
            "🤔 **Request Unclear**",
            "*I need more specific information to help you*\n",
            "**💡 What I can help with:**",
            "• **Create documents:** `Create a new customer`, `Make a sales order`",
            "• **List information:** `Show me all customers`, `List sales orders`",
            "• **Get details:** `Get customer details for CUST-001`",
            "• **Update records:** `Update customer CUST-001 set name to ABC Corp`",
            "• **Assign roles:** `Assign Sales User role to user@company.com`",
            "",
            "**📝 Example commands:**",
            "• `Create a new customer`",
            "• `Show me my sales orders`",
            "• `List all items`", 
            "• `Update customer ABC-001`",
            "• `Create a purchase order`",
            "",
            "**🎯 Document types I work with:**",
            "• Customer, Supplier, Item, Employee",
            "• Sales Order, Purchase Order, Quotation",
            "• Sales Invoice, Purchase Invoice",
            "• Stock Entry, Asset, Project, Task",
            "",
            "**💬 Try being more specific:**",
            "• Include the action you want (create, show, update, delete)",
            "• Mention the document type (customer, item, order, etc.)",
            "• Add any specific details or names"
        ]
        return "\n".join(response_parts)

    # Check permissions
    if not frappe.has_permission(doctype, "read"):
        return f"You don't have permission to access {doctype} documents."

    # Handle different actions with permission checking
    if action == "create":
        if not frappe.has_permission(doctype, "create"):
            return f"❌ You don't have permission to create {doctype} documents."
        return handle_create_action(doctype, task_json, user)
    
    elif action == "create_doctype":
        return handle_create_doctype_action(task_json, user, user_input)
    
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

def handle_create_doctype_action(task_json, user, user_input=""):
    """Handle DocType creation requests"""
    try:
        # Check if user has permission to create DocTypes
        if not frappe.has_permission("DocType", "create"):
            return "❌ You don't have permission to create DocTypes. This requires System Manager or Developer role."
        
        module = task_json.get("module", "")
        doctype_name = task_json.get("doctype_name", "")
        
        # Try to extract module from user input if not provided by Gemini
        if not module and user_input:
            user_input_lower = user_input.lower()
            if "franchise onboarding" in user_input_lower:
                module = "Franchise Onboarding"
            elif "module" in user_input_lower:
                # Try to extract module name after "module" keyword
                import re
                module_match = re.search(r'module\s+([a-zA-Z\s]+)', user_input_lower)
                if module_match:
                    potential_module = module_match.group(1).strip().title()
                    # Check if it's a valid module
                    available_modules = frappe.get_all("Module Def", fields=["name"])
                    module_names = [m.name for m in available_modules]
                    if potential_module in module_names:
                        module = potential_module
        
        # Create beautiful HTML response for DocType creation guide
        response_html = f"""
<div class="nexchat-field-container">
    <div class="nexchat-field-header">
        <span class="nexchat-field-icon">🏗️</span>
        <h4 class="nexchat-field-title">DocType Creation Guide</h4>
        <span class="nexchat-field-type">Development</span>
    </div>
    
    <div class="nexchat-help-section">
        <div class="nexchat-help-title">📋 Overview</div>
        <p>Creating a DocType involves defining fields, permissions, and business logic. Follow these steps to create your custom DocType.</p>
    </div>
    
    {f'''
    <div class="nexchat-options-header">
        <span class="nexchat-options-title">🎯 Target Module</span>
        <span class="nexchat-options-count">{module}</span>
    </div>
    ''' if module else ''}
    
    {f'''
    <div class="nexchat-options-header">
        <span class="nexchat-options-title">📝 DocType Name</span>
        <span class="nexchat-options-count">{doctype_name}</span>
    </div>
    ''' if doctype_name else ''}
    
    <div class="nexchat-help-section">
        <div class="nexchat-help-title">📋 Step-by-Step Instructions</div>
        
        <div class="nexchat-step-container">
            <div class="nexchat-step-number">1</div>
            <div class="nexchat-step-content">
                <h5 class="nexchat-step-title">Access DocType Creator</h5>
                <ul class="nexchat-help-list">
                    <li>Go to <strong>Developer</strong> → <strong>DocType</strong> → <strong>New</strong></li>
                    <li>Or use the search bar and type 'DocType'</li>
                </ul>
            </div>
        </div>
        
        <div class="nexchat-step-container">
            <div class="nexchat-step-number">2</div>
            <div class="nexchat-step-content">
                <h5 class="nexchat-step-title">Basic Configuration</h5>
                <ul class="nexchat-help-list">
                    <li><strong>Name:</strong> {doctype_name or 'YourDocTypeName'}</li>
                    <li><strong>Module:</strong> {module or 'Select appropriate module'}</li>
                    <li><strong>Description:</strong> Brief description of what this DocType represents</li>
                </ul>
            </div>
        </div>
        
        <div class="nexchat-step-container">
            <div class="nexchat-step-number">3</div>
            <div class="nexchat-step-content">
                <h5 class="nexchat-step-title">Add Fields</h5>
                <ul class="nexchat-help-list">
                    <li>Click <strong>Add Row</strong> in the Fields table</li>
                    <li>Configure field properties:</li>
                </ul>
                <div class="nexchat-sub-list">
                    <div class="nexchat-sub-item"><strong>Label:</strong> Display name for the field</div>
                    <div class="nexchat-sub-item"><strong>Type:</strong> Data, Link, Select, Date, etc.</div>
                    <div class="nexchat-sub-item"><strong>Field Name:</strong> Auto-generated or custom</div>
                    <div class="nexchat-sub-item"><strong>Options:</strong> For Link/Select fields</div>
                    <div class="nexchat-sub-item"><strong>Mandatory:</strong> Check if required</div>
                </div>
            </div>
        </div>
        
        <div class="nexchat-step-container">
            <div class="nexchat-step-number">4</div>
            <div class="nexchat-step-content">
                <h5 class="nexchat-step-title">Configure Permissions</h5>
                <ul class="nexchat-help-list">
                    <li>Go to <strong>Permissions</strong> section</li>
                    <li>Add roles that can access this DocType</li>
                    <li>Set Read, Write, Create, Delete permissions</li>
                </ul>
            </div>
        </div>
        
        <div class="nexchat-step-container">
            <div class="nexchat-step-number">5</div>
            <div class="nexchat-step-content">
                <h5 class="nexchat-step-title">Advanced Settings</h5>
                <ul class="nexchat-help-list">
                    <li><strong>Naming:</strong> Auto-generated, field-based, or custom</li>
                    <li><strong>Track Changes:</strong> Enable document versioning</li>
                    <li><strong>Search Fields:</strong> Fields to search by</li>
                </ul>
            </div>
        </div>
        
        <div class="nexchat-step-container">
            <div class="nexchat-step-number">6</div>
            <div class="nexchat-step-content">
                <h5 class="nexchat-step-title">Save and Test</h5>
                <ul class="nexchat-help-list">
                    <li>Click <strong>Save</strong> to create the DocType</li>
                    <li>Test by creating a new document</li>
                    <li>Modify fields and permissions as needed</li>
                </ul>
            </div>
        </div>
    </div>
    
    <div class="nexchat-help-section">
        <div class="nexchat-help-title">💡 Pro Tips</div>
        <ul class="nexchat-help-list">
            <li>Start with basic fields and add complexity later</li>
            <li>Use consistent naming conventions</li>
            <li>Test permissions with different user roles</li>
            <li>Consider workflow requirements early</li>
        </ul>
    </div>
    
    {f'''
    <div class="nexchat-help-section">
        <div class="nexchat-help-title">💻 Alternative - Command Line</div>
        <div class="nexchat-examples-grid">
            <div class="nexchat-example-item">bench new-doctype --app your_app_name --module '{module}' {doctype_name or 'YourDocType'}</div>
        </div>
    </div>
    ''' if module else ''}
</div>

<style>
.nexchat-step-container {{
    display: flex;
    margin: 12px 0;
    padding: 16px;
    border: 1px solid #e5e7eb;
    border-radius: 8px;
    background: #f9fafb;
}}

.nexchat-step-number {{
    flex-shrink: 0;
    width: 32px;
    height: 32px;
    background: #3b82f6;
    color: white;
    border-radius: 50%;
    display: flex;
    align-items: center;
    justify-content: center;
    font-weight: bold;
    font-size: 14px;
    margin-right: 16px;
}}

.nexchat-step-content {{
    flex: 1;
}}

.nexchat-step-title {{
    margin: 0 0 8px 0;
    font-size: 16px;
    font-weight: 600;
    color: #1f2937;
}}

.nexchat-sub-list {{
    margin-left: 20px;
    margin-top: 8px;
}}

.nexchat-sub-item {{
    padding: 4px 0;
    color: #6b7280;
    border-left: 2px solid #e5e7eb;
    padding-left: 12px;
    margin: 4px 0;
}}
</style>
"""
        
        return response_html
        
    except Exception as e:
        return f"Error processing DocType creation request: {str(e)}"

def handle_create_action(doctype, task_json, user):
    """Handle document creation"""
    try:
        # CRITICAL FIX: Ensure doctype is properly preserved from the start
        # Get data first as it's needed for conditional field checks
        data = task_json.get("data", {})
            
        # Get required fields for the doctype
        meta = frappe.get_meta(doctype)
        required_fields = []
        
        # Get base required fields from metadata
        for df in meta.fields:
            is_required = df.reqd and not df.hidden and not df.read_only and not df.default
            
            if is_required:
                # Skip standard fields that are auto-populated
                if df.fieldname not in ['name', 'owner', 'creation', 'modified', 'modified_by', 'docstatus']:
                    # For Stock Entry, skip series as it's auto-generated
                    if doctype == "Stock Entry" and df.fieldname == "naming_series":
                        continue
                    # CRITICAL FIX: Skip child table fields - they are handled separately
                    if df.fieldtype == "Table":
                        continue
                    required_fields.append(df.fieldname)

        # Add hardcoded required fields for specific doctypes
        if doctype == "Payment Entry":
            # These fields are required by business logic even if not marked reqd=1
            # Always add party_type and party - we'll filter out later for Internal Transfer
            if "party_type" not in required_fields:
                required_fields.append("party_type")
            if "party" not in required_fields:
                required_fields.append("party")
            
            # Filter out fields that are often auto-calculated or conditional
            # These fields are typically auto-calculated or only needed in specific scenarios
            fields_to_exclude = [
                "target_exchange_rate",  # Only needed for multi-currency
                "difference_amount",  # Auto-calculated
                "total_allocated_amount",  # Auto-calculated
                "unallocated_amount",  # Auto-calculated
                "base_paid_amount",  # Auto-calculated
                "base_received_amount",  # Auto-calculated
                "base_total_allocated_amount",  # Auto-calculated
                "base_unallocated_amount"  # Auto-calculated
            ]
            # Don't filter out received_amount yet - we'll auto-set it during field collection
            for field_to_exclude in fields_to_exclude:
                if field_to_exclude in required_fields:
                    required_fields.remove(field_to_exclude)
        
        # Remove duplicates while preserving order
        required_fields = list(dict.fromkeys(required_fields))
        missing_fields = []
        
        for field in required_fields:
            field_obj = meta.get_field(field)
            if field not in data:
                if field_obj and field_obj.default:
                    # Set the default value in data instead of adding to missing_fields
                    if field_obj.default == "Today":
                        from datetime import date
                        data[field] = date.today().strftime("%Y-%m-%d")
                    else:
                        data[field] = field_obj.default
                else:
                    # Handle smart defaults for specific doctypes
                    default_value = None
                    if doctype == "Payment Entry":
                        if field == "party_type":
                            # Auto-set party type based on payment type (if available)
                            if data.get("payment_type") == "Receive":
                                default_value = "Customer"
                            elif data.get("payment_type") == "Pay":
                                default_value = "Supplier"
                            elif data.get("payment_type") == "Internal Transfer":
                                # For Internal Transfer, we don't need party_type or party
                                continue  # Skip adding to missing_fields
                        elif field == "party" and data.get("payment_type") == "Internal Transfer":
                            # For Internal Transfer, we don't need party
                            continue  # Skip adding to missing_fields
                        elif field == "received_amount":
                            # Auto-set received_amount to paid_amount for same currency transactions
                            paid_amount = data.get("paid_amount")
                            source_rate = data.get("source_exchange_rate", 1)
                            if paid_amount:
                                if source_rate in [0, 0.0, 1, 1.0]:
                                    # Same currency or no conversion needed
                                    default_value = paid_amount
                                else:
                                    # Different currency - calculate received amount
                                    default_value = float(paid_amount) * float(source_rate)
                            else:
                                # If no paid_amount yet, we'll collect this field later
                                missing_fields.append(field)
                                continue
                        elif field == "target_exchange_rate":
                            # Auto-set target exchange rate
                            source_rate = data.get("source_exchange_rate", 1)
                            if source_rate in [0, 0.0, 1, 1.0]:
                                # Same currency
                                default_value = 1.0
                            else:
                                # For different currencies, default to 1 as well unless specified
                                default_value = 1.0
                    
                    if default_value:
                        data[field] = default_value
                    else:
                        missing_fields.append(field)
        
        # Get required child tables for the doctype
        required_child_tables = get_required_child_tables(doctype)
        missing_child_tables = []
        
        for child_table in required_child_tables:
            if child_table not in data or not data[child_table]:
                missing_child_tables.append(child_table)
        
        # Debug: Log child table status
        try:
            frappe.log_error(f"Child tables for {doctype}: required={required_child_tables}, missing={missing_child_tables}", "Child Table Status")
        except:
            pass
        
        # For Asset doctype, also check for conditionally mandatory fields
        if doctype == "Asset":
            # gross_purchase_amount is mandatory for non-composite assets
            if "gross_purchase_amount" not in data and "gross_purchase_amount" not in missing_fields:
                missing_fields.append("gross_purchase_amount")
        
        # For Purchase Invoice, ensure company is collected even if not strictly required
        if doctype == "Purchase Invoice":
            if "company" not in data and "company" not in missing_fields:
                missing_fields.append("company")

        if missing_fields or missing_child_tables:
            
            # Special handling for Stock Entry - ALWAYS show type selection first
            if doctype == "Stock Entry":
                return show_stock_entry_type_selection(data, missing_fields, user)
            
            # Handle regular fields first, then child tables
            if missing_fields:
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
            
        elif missing_child_tables:
            # All regular fields collected, now collect child tables
            child_table_to_collect = missing_child_tables[0]
            try:
                    frappe.log_error(f"Transitioning to child table: {child_table_to_collect} for {doctype}", "Child Table Transition")
            except:
                   pass
            return show_child_table_collection(doctype, child_table_to_collect, data, missing_child_tables, user)
        
        else:
            # All required fields and child tables are present, create the document
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
            
        # Stock Entry hardcoded logic removed - now handled by generic child table system
        else:
            # Default handling for other doctypes
            # Separate child table data from regular field data
            regular_data = {}
            child_table_data = {}
            
            for field_name, field_value in data.items():
                # Check if this is a child table field
                meta = frappe.get_meta(doctype)
                field_def = meta.get_field(field_name)
                
                if field_def and field_def.fieldtype == "Table":
                    child_table_data[field_name] = field_value
                else:
                    regular_data[field_name] = field_value
            
            # Enhanced validation for Link fields during creation
            meta = frappe.get_meta(doctype)
            for field_name, field_value in regular_data.items():
                field_def = meta.get_field(field_name)
                
                if field_def and field_def.fieldtype == "Link" and field_def.options and field_value:
                    link_doctype = field_def.options
                    
                    # Check if linked document exists with fuzzy matching
                    if not frappe.db.exists(link_doctype, field_value):
                        # Try fuzzy matching for linked document
                        linked_docs = frappe.get_all(link_doctype, fields=['name'], limit=50)
                        
                        # Try exact case-insensitive match first
                        matched_name = None
                        for linked_doc in linked_docs:
                            if linked_doc.name.lower() == field_value.lower():
                                matched_name = linked_doc.name
                                frappe.log_error(f"Create: Link fuzzy matched '{field_value}' to '{linked_doc.name}'", "Create Link Fuzzy Match")
                                break
                        
                        if not matched_name:
                            # Try partial match
                            partial_matches = [linked_doc.name for linked_doc in linked_docs 
                                             if field_value.lower() in linked_doc.name.lower() 
                                             or linked_doc.name.lower() in field_value.lower()]
                            
                            if len(partial_matches) == 1:
                                matched_name = partial_matches[0]
                                frappe.log_error(f"Create: Link partial matched '{field_value}' to '{matched_name}'", "Create Link Partial Match")
                            elif len(partial_matches) > 1:
                                match_list = ", ".join(partial_matches[:5])
                                clear_conversation_state(user)
                                return f"Multiple {link_doctype} found matching '{field_value}': {match_list}. Please be more specific."
                        
                        if matched_name:
                            regular_data[field_name] = matched_name  # Use the matched name
                        else:
                            clear_conversation_state(user)
                            return f"Could not find {link_doctype}: '{field_value}'. Please check the name and try again."
            
            # Special handling for Payment Entry before updating fields
            if doctype == "Payment Entry":
                # Auto-set received_amount if not provided but paid_amount exists
                if "received_amount" not in regular_data and "paid_amount" in regular_data:
                    paid_amount = regular_data.get("paid_amount", 0)
                    source_rate = regular_data.get("source_exchange_rate", 1)
                    
                    if source_rate in [0, 0.0, 1, 1.0]:
                        # Same currency or no conversion
                        regular_data["received_amount"] = paid_amount
                    else:
                        # Currency conversion
                        regular_data["received_amount"] = float(paid_amount) * float(source_rate)
                
                # Auto-set target_exchange_rate if not provided
                if "target_exchange_rate" not in regular_data:
                    regular_data["target_exchange_rate"] = 1.0
            
            # Update regular fields first
            doc.update(regular_data)
            
            # Add child table rows
            for table_field, rows in child_table_data.items():
                if isinstance(rows, list):
                    for row_data in rows:
                        # Set default delivery_date for Sales Order Items if not provided
                        if doctype == "Sales Order" and table_field == "items" and "delivery_date" not in row_data:
                            from datetime import date, timedelta
                            # Default to 7 days from today to ensure it's after sales order date
                            default_delivery_date = date.today() + timedelta(days=7)
                            row_data["delivery_date"] = default_delivery_date.strftime("%Y-%m-%d")
                            frappe.log_error(f"Auto-set delivery_date to {default_delivery_date.strftime('%Y-%m-%d')} for Sales Order Item", "Delivery Date Auto-Set")
                        
                        doc.append(table_field, row_data)
        
        # Insert the document
        doc.insert()
        frappe.db.commit()
        
        clear_conversation_state(user)
        # Create beautiful success message with heavy markdown styling
        response_parts = [
            f"🎉 **{doctype} Created Successfully!**",
            f"*Your new {doctype.lower()} is ready for use*\n",
            f"📋 **Document Details:**",
            f"• **{doctype} ID:** `{doc.name}`",
            f"• **Status:** ✅ Active and saved",
            f"• **Location:** Available in {doctype} list",
            "",
            f"**🚀 What's Next:**",
            f"• **View:** Check the {doctype} list to see your new document",
            f"• **Edit:** Make changes anytime via ERPNext interface", 
            f"• **Use:** This {doctype.lower()} is ready for transactions",
            "",
            f"**💡 Quick Access:**",
            f"• Navigate to **{doctype}** → **{doctype} List**",
            f"• Search for `{doc.name}` to find your document",
            f"• All fields have been saved successfully!"
        ]
        
        clear_conversation_state(user)
        return "\n".join(response_parts)
        
    except frappe.DuplicateEntryError:
        clear_conversation_state(user)
        # Create beautiful duplicate error message
        response_parts = [
            f"⚠️ **{doctype} Already Exists**",
            f"*A {doctype.lower()} with this information is already in the system*\n",
            f"**🔍 What happened:**",
            f"• A {doctype.lower()} with these details already exists",
            f"• ERPNext prevents duplicate entries automatically",
            f"• This helps maintain data integrity",
            "",
            f"**💡 What you can do:**",
            f"• **Check existing:** Look in the {doctype} list for similar entries",
            f"• **Modify details:** Try with different name or information",
            f"• **Update existing:** Edit the existing {doctype.lower()} instead",
            "",
            f"**🔧 Suggestions:**",
            f"• Use `List all {doctype.lower()}s` to see existing entries",
            f"• Try a different name or identifier",
            f"• Update the existing record if needed"
        ]
        return "\n".join(response_parts)
    except frappe.ValidationError as e:
        clear_conversation_state(user)
        # Create beautiful validation error message
        response_parts = [
            f"❌ **{doctype} Validation Failed**",
            f"*The {doctype.lower()} data didn't pass validation checks*\n",
            f"**🚨 Validation Error:**",
            f"• `{str(e)}`",
            "",
            f"**💡 Common solutions:**",
            f"• **Check required fields:** Ensure all mandatory fields are filled",
            f"• **Verify formats:** Dates, emails, numbers should be in correct format",
            f"• **Review permissions:** Check if you can create this {doctype.lower()}",
            f"• **Validate links:** Ensure linked documents exist",
            "",
            f"**🔧 Try again with:**",
            f"• Corrected field values",
            f"• All required information",
            f"• Proper data formats"
        ]
        return "\n".join(response_parts)
    except Exception as e:
        clear_conversation_state(user)
        # Create beautiful general error message
        response_parts = [
            f"💥 **{doctype} Creation Error**",
            f"*An unexpected error occurred while creating your {doctype.lower()}*\n",
            f"**🚨 Error Details:**",
            f"• `{str(e)}`",
            "",
            f"**💡 What to try:**",
            f"• **Retry:** Try creating the {doctype.lower()} again",
            f"• **Check data:** Verify all information is correct",
            f"• **Contact admin:** If the error persists",
            "",
            f"**🔧 Troubleshooting:**",
            f"• Check your permissions for {doctype}",
            f"• Ensure all required fields are provided",
            f"• Verify system connectivity"
        ]
        return "\n".join(response_parts)

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
            # Create beautiful no results message
            response_parts = [
                f"📋 **{doctype} List**",
                f"*No {doctype.lower()} documents found{filter_text}*\n",
                f"**🔍 Search Results:**",
                f"• **Found:** 0 {doctype.lower()}s",
                f"• **Filters:** {filters if filters else 'None applied'}",
                "",
                f"**💡 What you can do:**",
                f"• **Create new:** `Create a new {doctype.lower()}`",
                f"• **Remove filters:** Try without search criteria",
                f"• **Check spelling:** Verify filter values are correct",
                "",
                f"**🚀 Quick Actions:**",
                f"• Create your first {doctype.lower()}",
                f"• Import {doctype.lower()}s from spreadsheet",
                f"• Configure {doctype.lower()} settings"
            ]
            return "\n".join(response_parts)
        
        # Unicode circled numbers for beautiful badges (purple theme)
        circled_numbers = ["①", "②", "③", "④", "⑤", "⑥", "⑦", "⑧", "⑨", "⑩"]
        
        # Create beautiful document list
        response_parts = [
            f"📋 **{doctype} List**",
            f"*{len(docs)} most recent {doctype.lower()} documents*\n"
        ]
        
        # Add the beautiful document cards with circled numbers
        response_parts.append(f"**📄 Recent {doctype}s:**")
        for i, doc in enumerate(docs, 1):
            badge = circled_numbers[i-1] if i <= len(circled_numbers) else f"({i})"
            # Format modified date nicely
            from datetime import datetime
            try:
                mod_date = doc.modified.strftime("%b %d, %Y") if hasattr(doc.modified, 'strftime') else str(doc.modified)[:10]
            except:
                mod_date = "Recent"
            response_parts.append(f"{badge} **{doc.name}** *({mod_date})*")
        
        response_parts.extend([
            "",
            f"**📊 List Summary:**",
            f"• **Total Shown:** {len(docs)} {doctype.lower()}s",
            f"• **Order:** Most recent first",
            f"• **Status:** All active documents",
            "",
            f"**💡 More Actions:**",
            f"• **Get details:** `Get {doctype.lower()} [name]`",
            f"• **Update:** `Update {doctype.lower()} [name] set [field] to [value]`",
            f"• **Create new:** `Create a new {doctype.lower()}`",
            "",
            f"**🔍 Navigation:**",
            f"• Ask about specific {doctype.lower()}s by name",
            f"• Use ERPNext interface for full list view",
            f"• Filter results with specific criteria"
        ])
        
        return "\n".join(response_parts)
        
    except Exception as e:
        # Create beautiful error message
        response_parts = [
            f"💥 **{doctype} List Error**",
            f"*Error retrieving {doctype.lower()} documents*\n",
            f"**🚨 Error Details:**",
            f"• `{str(e)}`",
            "",
            f"**💡 Try these solutions:**",
            f"• **Retry:** Ask for the list again",
            f"• **Check permissions:** Ensure you can view {doctype.lower()}s",
            f"• **Contact admin:** If error persists",
            "",
            f"**🔧 Alternative:**",
            f"• Access {doctype} list via ERPNext interface",
            f"• Use different search criteria",
            f"• Check system connectivity"
        ]
        return "\n".join(response_parts)

def handle_get_action(doctype, task_json):
    """Handle getting specific document information"""
    try:
        filters = task_json.get("filters", {})
        field = task_json.get("field")
        
        if not filters:
            # Create beautiful help message for missing filters
            response_parts = [
                f"🔍 **Get {doctype} Information**",
                f"*Specify which {doctype.lower()} you'd like details about*\n",
                f"**💡 How to specify:**",
                f"• **By name:** `Get {doctype.lower()} [document-name]`",
                f"• **By ID:** `Get {doctype.lower()} [ID]`",
                f"• **Specific field:** `Get [field] for {doctype.lower()} [name]`",
                "",
                f"**📝 Examples:**",
                f"• `Get {doctype.lower()} CUST-001`",
                f"• `Get customer_name for {doctype.lower()} CUST-001`",
                f"• `Show {doctype.lower()} details for [name]`",
                "",
                f"**🎯 What I can show:**",
                f"• All field values for the {doctype.lower()}",
                f"• Specific field information",
                f"• Document status and details"
            ]
            return "\n".join(response_parts)
        
        doc = frappe.get_doc(doctype, filters)
        
        if field and hasattr(doc, field):
            value = getattr(doc, field)
            # Create beautiful single field response
            response_parts = [
                f"🔍 **{doctype} Field Information**",
                f"*{field} value for {doctype.lower()} '{doc.name}'*\n",
                f"**📋 Field Details:**",
                f"• **Document:** {doc.name}",
                f"• **Field:** {field}",
                f"• **Value:** `{value}`",
                "",
                f"**💡 More actions:**",
                f"• **Full details:** `Get {doctype.lower()} {doc.name}`",
                f"• **Update field:** `Update {doctype.lower()} {doc.name} set {field} to [new_value]`",
                f"• **List all:** `Show all {doctype.lower()}s`"
            ]
            return "\n".join(response_parts)
        else:
            # Return basic info about the document with beautiful formatting
            info_fields = ["name"]
            meta = frappe.get_meta(doctype)
            
            # Add some commonly useful fields
            for df in meta.fields[:8]:  # Show more fields
                if not df.hidden and df.fieldtype not in ["Section Break", "Column Break", "HTML", "Table"]:
                    info_fields.append(df.fieldname)
            
            # Create beautiful document details response
            response_parts = [
                f"📄 **{doctype} Details**",
                f"*Complete information for {doctype.lower()} '{doc.name}'*\n",
                f"**📋 Document Information:**"
            ]
            
            field_count = 0
            for field_name in info_fields:
                if hasattr(doc, field_name):
                    value = getattr(doc, field_name)
                    if value:
                        field_obj = meta.get_field(field_name)
                        field_label = field_obj.label if field_obj else field_name.replace("_", " ").title()
                        response_parts.append(f"• **{field_label}:** `{value}`")
                        field_count += 1
                        if field_count >= 10:  # Limit to 10 fields for chat display
                            break
            
            response_parts.extend([
                "",
                f"**📊 Document Summary:**",
                f"• **Type:** {doctype}",
                f"• **ID:** {doc.name}",
                f"• **Fields shown:** {field_count} of {len(meta.fields)} total",
                "",
                f"**💡 Available actions:**",
                f"• **Update:** `Update {doctype.lower()} {doc.name} set [field] to [value]`",
                f"• **List related:** `Show all {doctype.lower()}s`", 
                f"• **Create similar:** `Create a new {doctype.lower()}`",
                "",
                f"**🔧 Access full details:**",
                f"• Navigate to {doctype} → {doc.name} in ERPNext",
                f"• Use the web interface for complete view",
                f"• Export data for external analysis"
            ])
            
            return "\n".join(response_parts)
            
    except frappe.DoesNotExistError:
        # Create beautiful not found message
        response_parts = [
            f"❌ **{doctype} Not Found**",
            f"*Could not locate the requested {doctype.lower()}*\n",
            f"**🔍 Search criteria:**",
            f"• **Filters:** {filters}",
            f"• **Doctype:** {doctype}",
            "",
            f"**💡 Possible reasons:**",
            f"• **Wrong name:** Check the {doctype.lower()} name/ID spelling",
            f"• **Deleted:** The {doctype.lower()} may have been removed",
            f"• **Permissions:** You might not have access to view it",
            "",
            f"**🔧 Try these solutions:**",
            f"• **Check spelling:** Verify the {doctype.lower()} name is correct",
            f"• **List all:** Use `Show all {doctype.lower()}s` to see available ones",
            f"• **Contact admin:** If you should have access to this {doctype.lower()}"
        ]
        return "\n".join(response_parts)
    except Exception as e:
        # Create beautiful error message
        response_parts = [
            f"💥 **{doctype} Retrieval Error**",
            f"*Error getting {doctype.lower()} information*\n",
            f"**🚨 Error Details:**",
            f"• `{str(e)}`",
            "",
            f"**💡 What to try:**",
            f"• **Retry:** Ask for the {doctype.lower()} again",
            f"• **Check name:** Verify the {doctype.lower()} name is correct",
            f"• **Check permissions:** Ensure you can view {doctype.lower()}s",
            "",
            f"**🔧 Alternative:**",
            f"• Use the ERPNext interface to access {doctype}",
            f"• Try listing all {doctype.lower()}s first",
            f"• Contact system administrator"
        ]
        return "\n".join(response_parts)

def handle_update_action(doctype, task_json, user):
    """Handle document updates"""
    try:
        # Fix for common doctype name issues (spaces missing)
        doctype_mappings = {
            "ItemGroup": "Item Group",
            "CustomerGroup": "Customer Group", 
            "SupplierGroup": "Supplier Group",
            "SalesOrder": "Sales Order",
            "PurchaseOrder": "Purchase Order",
            "SalesInvoice": "Sales Invoice",
            "PurchaseInvoice": "Purchase Invoice",
            "StockEntry": "Stock Entry",
            "CostCenter": "Cost Center",
            "WarehouseType": "Warehouse Type"
        }
        
        # Check if we need to map the doctype name
        if doctype in doctype_mappings:
            original_doctype = doctype
            doctype = doctype_mappings[doctype]
            frappe.log_error(f"Mapped doctype '{original_doctype}' to '{doctype}'", "Doctype Mapping")
        
        filters = task_json.get("filters", {})
        data = task_json.get("data", {})
        field_to_update = task_json.get("field_to_update")  # For partial updates
        
        if not filters:
            return f"Please specify which {doctype} document you want to update. For example: 'Update customer CUST-001 set customer_name to New Name'"
        
        # Enhanced document existence check with fuzzy matching
        if not frappe.db.exists(doctype, filters):
            # Try fuzzy matching for document names
            if 'name' in filters:
                search_name = filters['name']
                # Get all documents of this type for fuzzy matching
                all_docs = frappe.get_all(doctype, fields=['name'], limit=50)
                
                # Try exact case-insensitive match first
                for doc_info in all_docs:
                    if doc_info.name.lower() == search_name.lower():
                        filters['name'] = doc_info.name  # Use exact name
                        frappe.log_error(f"Fuzzy matched '{search_name}' to '{doc_info.name}'", "Fuzzy Match")
                        break
                else:
                    # Try partial match
                    partial_matches = [doc_info.name for doc_info in all_docs 
                                     if search_name.lower() in doc_info.name.lower() 
                                     or doc_info.name.lower() in search_name.lower()]
                    
                    if len(partial_matches) == 1:
                        filters['name'] = partial_matches[0]
                        frappe.log_error(f"Partial matched '{search_name}' to '{partial_matches[0]}'", "Partial Match")
                    elif len(partial_matches) > 1:
                        match_list = ", ".join(partial_matches[:5])
                        return f"Multiple {doctype} documents found matching '{search_name}': {match_list}. Please be more specific."
            
            # Final check after fuzzy matching
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
        
        # Enhanced field update with link field validation and fuzzy matching
        updated_fields = []
        for field, value in data.items():
            if hasattr(doc, field):
                old_value = getattr(doc, field)
                
                # Special handling for Link fields - validate and fuzzy match
                meta = frappe.get_meta(doctype)
                field_def = meta.get_field(field)
                
                if field_def and field_def.fieldtype == "Link" and field_def.options and value:
                    link_doctype = field_def.options
                    
                    # Check if linked document exists
                    if not frappe.db.exists(link_doctype, value):
                        # Try fuzzy matching for linked document
                        linked_docs = frappe.get_all(link_doctype, fields=['name'], limit=50)
                        
                        # Try exact case-insensitive match first
                        matched_name = None
                        for linked_doc in linked_docs:
                            if linked_doc.name.lower() == value.lower():
                                matched_name = linked_doc.name
                                frappe.log_error(f"Link fuzzy matched '{value}' to '{linked_doc.name}'", "Link Fuzzy Match")
                                break
                        
                        if not matched_name:
                            # Try partial match
                            partial_matches = [linked_doc.name for linked_doc in linked_docs 
                                             if value.lower() in linked_doc.name.lower() 
                                             or linked_doc.name.lower() in value.lower()]
                            
                            if len(partial_matches) == 1:
                                matched_name = partial_matches[0]
                                frappe.log_error(f"Link partial matched '{value}' to '{matched_name}'", "Link Partial Match")
                            elif len(partial_matches) > 1:
                                match_list = ", ".join(partial_matches[:5])
                                clear_conversation_state(user)
                                return f"Multiple {link_doctype} found matching '{value}': {match_list}. Please be more specific."
                        
                        if matched_name:
                            value = matched_name  # Use the matched name
                        else:
                            return f"Could not find {link_doctype}: '{value}'. Please check the name and try again."
                
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
        
        # Create beautiful success message for updates
        response_parts = [
            f"✅ **{doctype} Updated Successfully!**",
            f"*Changes have been saved to {doctype.lower()} '{doc.name}'*\n",
            f"**📝 Updated Fields:**"
        ]
        
        # Add updated fields with beautiful formatting
        for field in updated_fields:
            response_parts.append(f"• {field}")
        
        response_parts.extend([
            "",
            f"**📊 Update Summary:**",
            f"• **Document:** {doc.name}",
            f"• **Fields changed:** {len(updated_fields)}",
            f"• **Status:** ✅ All changes saved",
            "",
            f"**💡 What's next:**",
            f"• **View:** Check the updated {doctype.lower()} in ERPNext",
            f"• **More updates:** Make additional changes anytime",
            f"• **Verify:** Review the changes in the document",
            "",
            f"**🔧 Quick actions:**",
            f"• **Get details:** `Get {doctype.lower()} {doc.name}`",
            f"• **List all:** `Show all {doctype.lower()}s`",
            f"• **Create new:** `Create a new {doctype.lower()}`"
        ])
        
        return "\n".join(response_parts)
        
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
        
        # Create beautiful delete success message
        response_parts = [
            f"🗑️ **{doctype} Deleted Successfully!**",
            f"*{doctype} '{doc_name}' has been permanently removed*\n",
            f"**📋 Deletion Details:**",
            f"• **Document:** {doc_name}",
            f"• **Type:** {doctype}",
            f"• **Status:** ✅ Permanently deleted",
            "",
            f"**💡 What happened:**",
            f"• The {doctype.lower()} has been removed from the system",
            f"• All data associated with '{doc_name}' is deleted",
            f"• This action cannot be undone",
            "",
            f"**🚀 What's next:**",
            f"• **Create new:** `Create a new {doctype.lower()}`",
            f"• **List others:** `Show all {doctype.lower()}s`",
            f"• **Import data:** Restore from backup if needed",
            "",
            f"**⚠️ Important note:**",
            f"• Deletion is permanent and cannot be reversed",
            f"• Check for any linked documents that may be affected",
            f"• Consider data backup procedures for future"
        ]
        return "\n".join(response_parts)
        
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
        
        # Create beautiful role assignment success message
        response_parts = [
            f"🎉 **Role Assigned Successfully!**",
            f"*'{role_name}' role has been granted to {user_email}*\n",
            f"**👤 Assignment Details:**",
            f"• **User:** {user_email}",
            f"• **Role:** {role_name}",
            f"• **Status:** ✅ Active and effective immediately",
            "",
            f"**🔐 What this means:**",
            f"• User can now access {role_name} features",
            f"• Permissions are active across all modules",
            f"• Access level increased as per role definition",
            "",
            f"**💡 Next steps:**",
            f"• **Verify:** User should log out and log back in",
            f"• **Test:** Check new permissions are working",
            f"• **Assign more:** Add additional roles if needed",
            "",
            f"**🔧 Additional actions:**",
            f"• **View all roles:** `Show all roles`",
            f"• **Assign more roles:** `Assign [role] to {user_email}`",
            f"• **List users:** `Show all users`"
        ]
        return "\n".join(response_parts)
        
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
        
        # Build beautiful response message with heavy markdown styling
        main_response_parts = [
            f"🎯 **Multiple Roles Assignment Complete!**",
            f"*Role assignment results for {user_email}*\n"
        ]
        
        if assigned_roles:
            main_response_parts.extend([
                f"✅ **Successfully Assigned ({len(assigned_roles)} roles):**"
            ])
            for i, role in enumerate(assigned_roles, 1):
                badge = f"✓" 
                main_response_parts.append(f"   {badge} **{role}**")
        
        if already_assigned:
            main_response_parts.extend([
                "",
                f"📋 **Already Assigned ({len(already_assigned)} roles):**"
            ])
            for role in already_assigned:
                main_response_parts.append(f"   ℹ️ **{role}** *(was already active)*")
        
        if failed_roles:
            main_response_parts.extend([
                "",
                f"❌ **Assignment Failed ({len(failed_roles)} roles):**"
            ])
            for role in failed_roles:
                main_response_parts.append(f"   ❌ **{role}**")
        
        if not assigned_roles and not already_assigned and not failed_roles:
            return f"No changes made to user '{user_email}' roles."
        
        # Add summary section
        total_active = len(assigned_roles) + len(already_assigned)
        main_response_parts.extend([
            "",
            f"**📊 Assignment Summary:**",
            f"• **User:** {user_email}",
            f"• **New roles:** {len(assigned_roles)}",
            f"• **Total active roles:** {total_active}+",
            f"• **Status:** ✅ All changes saved",
            "",
            f"**🔐 User permissions:**",
            f"• **Immediate effect:** All new roles are active now",
            f"• **Access level:** Significantly enhanced",
            f"• **Module access:** Expanded across ERPNext",
            "",
            f"**💡 Next steps:**",
            f"• **User action:** Log out and log back in to see changes",
            f"• **Verification:** Test new permissions and access",
            f"• **Documentation:** Record role assignments for audit"
        ])
        
        return "\n".join(main_response_parts)
        
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
    """Show beautiful stock entry type selection with heavy markdown styling"""
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
        
        # Unicode circled numbers for beautiful badges (purple theme)
        circled_numbers = ["①", "②", "③", "④", "⑤", "⑥", "⑦", "⑧", "⑨", "⑩"]
        
        current_number = 1
        
        # Create beautiful response with heavy markdown styling
        response_parts = [
            "🎯 **Select Stock Entry Type**",
            f"*Choose from {len(stock_entry_types)} stock operations*\n"
        ]
        
        # Add beautiful sections with circled numbers
        if inbound_types:
            response_parts.append("**📥 Inbound Operations:**")
            for entry_type in inbound_types:
                badge = circled_numbers[current_number-1] if current_number <= len(circled_numbers) else f"({current_number})"
                response_parts.append(f"{badge} **{entry_type}** - *Receive materials into warehouse*")
                current_number += 1
            response_parts.append("")
        
        if outbound_types:
            response_parts.append("**📤 Outbound Operations:**")
            for entry_type in outbound_types:
                badge = circled_numbers[current_number-1] if current_number <= len(circled_numbers) else f"({current_number})"
                description = "Issue materials from warehouse" if entry_type == "Material Issue" else "Send materials to subcontractor"
                response_parts.append(f"{badge} **{entry_type}** - *{description}*")
                current_number += 1
            response_parts.append("")
        
        if transfer_types:
            response_parts.append("**🔄 Transfer Operations:**")
            for entry_type in transfer_types:
                badge = circled_numbers[current_number-1] if current_number <= len(circled_numbers) else f"({current_number})"
                description = "Move materials between warehouses" if entry_type == "Material Transfer" else "Transfer for manufacturing processes"
                response_parts.append(f"{badge} **{entry_type}** - *{description}*")
                current_number += 1
            response_parts.append("")
        
        if production_types:
            response_parts.append("**🏭 Production Operations:**")
            for entry_type in production_types:
                badge = circled_numbers[current_number-1] if current_number <= len(circled_numbers) else f"({current_number})"
                description = "Manufacturing & production" if entry_type == "Manufacture" else "Repackaging operations"
                response_parts.append(f"{badge} **{entry_type}** - *{description}*")
                current_number += 1
            response_parts.append("")
        
        response_parts.extend([
            "**💡 How to select:**",
            "• Type a **number** (e.g., `2`) for your choice",
            "• Type the **operation name** directly",
            "• Type `cancel` to cancel operation",
            "",
            "**📝 Quick Examples:**",
            "• `2` → Select Material Receipt",
            "• `Material Transfer` → Direct selection",
            "• `cancel` → Cancel this operation",
            "",
            "**ℹ️ Operation Categories:**",
            "• **📥 Inbound:** Receive materials into warehouse",
            "• **📤 Outbound:** Issue materials from warehouse",
            "• **🔄 Transfer:** Move materials between warehouses",
            "• **🏭 Production:** Manufacturing & repackaging operations",
            "",
            f"**🎯 Stock Entry Selection:**",
            f"• **Total Operations:** {len(stock_entry_types)} available",
            f"• **Categories:** 4 operation types",
            f"• **Usage:** Essential for inventory management",
            f"• **Impact:** Updates stock levels automatically"
        ])
        
        response_text = "\n".join(response_parts)
        
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
        
        return response_text
        
    except Exception as e:
        return f"Error showing stock entry type selection: {str(e)}"

def show_company_selection(data, missing_fields, user, current_doctype=None):
    """Show simple company selection interface"""
    try:
        # Get available companies
        companies = frappe.get_all("Company", 
                                 fields=["name"],
                                 order_by="name")
        
        company_names = [comp.name for comp in companies]
        
        if not company_names:
            return """🏢 Select Company

No companies found in the system.

You can:
• Type a company name directly
• Type 'cancel' to cancel"""
        
        # Unicode circled numbers for beautiful badges (purple theme)
        circled_numbers = ["①", "②", "③", "④", "⑤", "⑥", "⑦", "⑧", "⑨", "⑩", "⑪", "⑫", "⑬", "⑭", "⑮", "⑯", "⑰", "⑱", "⑲", "⑳"]
        
        # Create beautiful response with heavy markdown styling
        response_parts = [
            "🏢 **Select Company**",
            f"*Choose from {len(companies)} registered companies*\n"
        ]
        
        # Add the beautiful company cards with circled numbers
        response_parts.append("**🏭 Available Companies:**")
        for i, company in enumerate(companies, 1):
            badge = circled_numbers[i-1] if i <= len(circled_numbers) else f"({i})"
            response_parts.append(f"{badge} **{company.name}**")
        
        response_parts.extend([
            "",
            "**💡 How to select:**",
            "• Type a **number** (e.g., `2`) for your choice",
            "• Type the **company name** directly",
            "• Type `cancel` to cancel operation",
            "",
            "**📝 Quick Examples:**",
            f"• `1` → Select **{companies[0].name}**" if companies else "",
            f"• `{companies[0].name}` → Select by name" if companies else "",
            "• `cancel` → Cancel this operation",
            "",
            f"**🎯 Company Selection:**",
            f"• **Total Companies:** {len(companies)} available",
            f"• **Field Type:** Company Link",
            f"• **Usage:** This company will be used for all transactions",
            f"• **Status:** Required for document creation"
        ])
        
        response_text = "\n".join([part for part in response_parts if part])
        
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
        
        return response_text
        
    except Exception as e:
        return f"Error showing company selection: {str(e)}"

# show_items_selection function removed - replaced by generic child table system

def show_warehouse_selection(field_name, data, missing_fields, user):
    """Show beautiful warehouse selection with HTML styling"""
    try:
        # Get available warehouses
        warehouses = frappe.get_all("Warehouse", 
                                  fields=["name", "warehouse_name"],
                                  order_by="name")
        
        # Get field label for display
        meta = frappe.get_meta("Stock Entry")
        field_obj = meta.get_field(field_name)
        field_label = field_obj.label or field_name.replace("_", " ").title()
        
        if not warehouses:
            return f"""🏪 **Select {field_label}**

**ℹ️ No Warehouses Available**

**💡 What you can do:**
• Type a **warehouse name** directly
• Type `cancel` to cancel operation
• Contact administrator to configure warehouses

**🔧 Field Information:**
• **Field:** {field_label}
• **Type:** Warehouse Link
• **Status:** No warehouses found"""
        
        # Unicode circled numbers for beautiful badges (purple theme)
        circled_numbers = ["①", "②", "③", "④", "⑤", "⑥", "⑦", "⑧", "⑨", "⑩", "⑪", "⑫", "⑬", "⑭", "⑮", "⑯", "⑰", "⑱", "⑲", "⑳"]
        
        # Create beautiful response with heavy markdown styling
        warehouse_names = []
        response_parts = [
            f"🏪 **Select {field_label}**",
            f"*Choose from {len(warehouses)} available warehouses*\n"
        ]
        
        # Add the beautiful warehouse cards with circled numbers
        response_parts.append("**📦 Available Warehouses:**")
        for i, warehouse in enumerate(warehouses, 1):
            display_name = warehouse.name
            if warehouse.warehouse_name and warehouse.warehouse_name != warehouse.name:
                display_name += f" *({warehouse.warehouse_name})*"
            badge = circled_numbers[i-1] if i <= len(circled_numbers) else f"({i})"
            response_parts.append(f"{badge} **{display_name}**")
            warehouse_names.append(warehouse.name)
        
        response_parts.extend([
            "",
            "**💡 How to select:**",
            "• Type a **number** (e.g., `3`) for your choice",
            "• Type the **warehouse name** directly",
            "• Type `cancel` to cancel operation",
            "",
            "**📝 Quick Examples:**",
            f"• `1` → Select **{warehouses[0].name}**" if warehouses else "",
            f"• `{warehouses[0].name}` → Select by exact name" if warehouses else "",
            "• `cancel` → Cancel this operation",
            "",
            f"**🎯 Warehouse Selection Details:**",
            f"• **Field:** {field_label}",
            f"• **Type:** Warehouse Link",
            f"• **Available:** {len(warehouses)} warehouses",
            f"• **Usage:** For stock operations and inventory management"
        ])
        
        response_text = "\n".join([part for part in response_parts if part])
        
        # Save state
        state = {
            "action": "collect_stock_selection",
            "selection_type": field_name,
            "data": data,
            "missing_fields": missing_fields,
            "numbered_options": warehouse_names
        }
        set_conversation_state(user, state)
        
        return response_text
        
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
        
        # Unicode circled numbers for beautiful badges (purple theme)
        circled_numbers = ["①", "②", "③", "④", "⑤", "⑥", "⑦", "⑧", "⑨", "⑩", "⑪", "⑫", "⑬", "⑭", "⑮", "⑯", "⑰", "⑱", "⑲", "⑳"]
        
        # Create beautiful response with heavy markdown styling
        response_parts = [
            "🏭 **Select Asset Item**",
            f"*Choose from {len(items)} asset-compatible items*\n" if items else "*No items available in system*\n"
        ]
        
        if items:
            item_codes = []
            
            # Add the beautiful item cards with circled numbers
            response_parts.append("**📦 Available Asset Items:**")
            for i, item in enumerate(items, 1):
                item_display = item.item_code
                if item.item_name and item.item_name != item.item_code:
                    item_display += f" *({item.item_name})*"
                badge = circled_numbers[i-1] if i <= len(circled_numbers) else f"({i})"
                response_parts.append(f"{badge} **{item_display}**")
                item_codes.append(item.item_code)
            
            response_parts.extend([
                "",
                "**💡 How to select:**",
                "• Type a **number** (e.g., `3`) for your choice",
                "• Type the **item code** directly",
                "• Type `cancel` to cancel operation",
                "",
                "**📝 Quick Examples:**",
                f"• `1` → Select **{items[0].item_code}**" if items else "",
                f"• `{items[0].item_code}` → Select by exact code" if items else "",
                "• `cancel` → Cancel this operation",
                "",
                f"**🎯 Asset Item Selection Details:**",
                f"• **Field:** Item Code (Asset)",
                f"• **Type:** Item Link",
                f"• **Available:** {len(items)} asset items",
                f"• **Filter:** Fixed asset items only"
            ])
        else:
            response_parts.extend([
                "**ℹ️ No Asset Items Available**",
                "",
                "**💡 What you can do:**",
                "• Type an **item code** directly",
                "• Type `cancel` to cancel operation",
                "• Create asset items in Item master first",
                "",
                "**🔧 Item Information:**",
                "• **Field:** Item Code",
                "• **Type:** Item Link (Asset)",
                "• **Status:** No asset items found"
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
        
        # Unicode circled numbers for beautiful badges (purple theme)
        circled_numbers = ["①", "②", "③", "④", "⑤", "⑥", "⑦", "⑧", "⑨", "⑩", "⑪", "⑫", "⑬", "⑭", "⑮", "⑯", "⑰", "⑱", "⑲", "⑳"]
        
        # Create beautiful response with heavy markdown styling
        response_parts = [
            "📍 **Select Asset Location**",
            f"*Choose from {len(locations)} available locations*\n" if locations else "*No locations found in system*\n"
        ]
        
        if locations:
            location_names = []
            
            # Add the beautiful location cards with circled numbers
            response_parts.append("**🏢 Available Locations:**")
            for i, location in enumerate(locations, 1):
                location_display = location.name
                if location.location_name and location.location_name != location.name:
                    location_display += f" *({location.location_name})*"
                badge = circled_numbers[i-1] if i <= len(circled_numbers) else f"({i})"
                response_parts.append(f"{badge} **{location_display}**")
                location_names.append(location.name)
            
            response_parts.extend([
                "",
                "**💡 How to select:**",
                "• Type a **number** (e.g., `3`) for your choice",
                "• Type the **location name** directly",
                "• Type `new location name` to create it",
                "• Type `cancel` to cancel operation",
                "",
                "**📝 Quick Examples:**",
                f"• `1` → Select **{locations[0].name}**" if locations else "",
                f"• `{locations[0].name}` → Select by exact name" if locations else "",
                "• `Main Office` → Create new location",
                "• `cancel` → Cancel this operation",
                "",
                f"**🎯 Asset Location Details:**",
                f"• **Field:** Location",
                f"• **Type:** Location Link",
                f"• **Available:** {len(locations)} locations",
                f"• **Feature:** Can create new locations instantly"
            ])
        else:
            response_parts.extend([
                "**ℹ️ No Locations Available**",
                "",
                "**💡 What you can do:**",
                "• Type a **location name** to create it",
                "• Type `cancel` to cancel operation",
                "• Example: `Main Office`, `Warehouse 1`, `Factory Floor`",
                "",
                "**🔧 Location Information:**",
                "• **Field:** Location",
                "• **Type:** Location Link",
                "• **Status:** No locations found",
                "• **Feature:** Auto-create new locations"
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
        
        # Unicode circled numbers for beautiful badges (purple theme)
        circled_numbers = ["①", "②", "③", "④", "⑤", "⑥", "⑦", "⑧", "⑨", "⑩", "⑪", "⑫", "⑬", "⑭", "⑮", "⑯", "⑰", "⑱", "⑲", "⑳"]
        
        # Create beautiful response with heavy markdown styling
        response_parts = [
            f"{icon} **Select {field_label}**",
            f"*Choose from {len(field_data)} available {field_label.lower()}s*\n" if field_data else f"*No {field_label.lower()}s found in system*\n"
        ]
        
        if field_data:
            field_options = []
            
            # Add the beautiful option cards with circled numbers
            if field_name == "asset_category":
                response_parts.append("**🏷️ Available Asset Categories:**")
            elif field_name == "asset_owner":
                response_parts.append("**👤 Available Asset Owners:**")
            else:
                response_parts.append(f"**📋 Available {field_label}s:**")
            
            for i, item in enumerate(field_data, 1):
                item_display = item.name
                # Use the second field as display name if available
                if len(item) > 1:
                    second_field = list(item.values())[1]
                    if second_field and second_field != item.name:
                        item_display += f" *({second_field})*"
                badge = circled_numbers[i-1] if i <= len(circled_numbers) else f"({i})"
                response_parts.append(f"{badge} **{item_display}**")
                field_options.append(item.name)
            
            response_parts.extend([
                "",
                "**💡 How to select:**",
                "• Type a **number** (e.g., `3`) for your choice",
                "• Type the **name** directly",
                "• Type `cancel` to cancel operation",
                "",
                "**📝 Quick Examples:**",
                f"• `1` → Select **{field_data[0].name}**" if field_data else "",
                f"• `{field_data[0].name}` → Select by exact name" if field_data else "",
                "• `cancel` → Cancel this operation",
                "",
                f"**🎯 {field_label} Selection Details:**",
                f"• **Field:** {field_label}",
                f"• **Type:** Link Field",
                f"• **Available:** {len(field_data)} {field_label.lower()}s",
                f"• **Usage:** Required for asset management"
            ])
        else:
            response_parts.extend([
                f"**ℹ️ No {field_label}s Available**",
                "",
                "**💡 What you can do:**",
                "• Type a **name** directly",
                "• Type `cancel` to cancel operation",
                f"• Create {field_label.lower()}s in master data first",
                "",
                f"**🔧 {field_label} Information:**",
                f"• **Field:** {field_label}",
                f"• **Type:** Link Field",
                f"• **Status:** No {field_label.lower()}s found"
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
        # Create beautiful response with heavy markdown styling
        response_parts = [
            "💰 **Enter Asset Purchase Amount**",
            "*Input the cost at which the asset was purchased*\n"
        ]
        
        # Add beautiful examples section
        response_parts.extend([
            "**📝 Amount Examples:**",
            "• `50000` → ₹50,000 (Standard format)",
            "• `25000.50` → ₹25,000.50 (With decimals)",
            "• `100000` → ₹100,000 (Large amount)",
            ""
        ])
        
        response_parts.extend([
            "**💰 Asset Purchase Amount Guidelines:**",
            "• **Whole amounts:** `50000`, `100000`, `250000`",
            "• **Decimal amounts:** `25000.50`, `99999.99`",
            "• **Large amounts:** `1000000` (1 million), `5000000`",
            "• **Zero amount:** `0` if no purchase cost",
            "",
            "**✅ Valid Format Examples:**",
            "• `50000` → Fifty thousand rupees",
            "• `25000.50` → Twenty-five thousand and fifty paise",
            "• `1000000` → Ten lakh rupees",
            "",
            "**❌ Invalid Formats:**",
            "• ~~`₹50000`~~ (No currency symbol needed)",
            "• ~~`50,000`~~ (No commas allowed)",
            "• ~~`50k`~~ (No abbreviations)",
            "",
            "**💡 How to enter:**",
            "• Type the **amount as a number** directly",
            "• Use **decimal point** for paise (e.g., `25000.50`)",
            "• Type `0` if **no purchase cost** or unknown",
            "• Type `cancel` to cancel operation",
            "",
            "**🎯 Asset Amount Details:**",
            "• **Field:** Gross Purchase Amount",
            "• **Type:** Currency Amount",
            "• **Format:** Decimal number (no symbols)",
            "• **Usage:** Used for depreciation calculations"
        ])
        
        response_text = "\n".join([part for part in response_parts if part])
        
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
        
        return response_text
        
    except Exception as e:
        return f"Error showing purchase amount selection: {str(e)}"

def show_generic_link_selection(field_name, field_label, link_doctype, data, missing_fields, user, current_doctype):
    """Show simple selection for any Link field"""
    try:
        # Check if this doctype should use pagination (based on likely record count)
        pagination_doctypes = ["Currency", "Customer", "Supplier", "Item", "Employee", "User", "Contact", "Address"]
        
        # Get total count to decide if pagination is needed
        total_count = frappe.db.count(link_doctype)
        
        if link_doctype in pagination_doctypes or total_count > 25:
            # Use paginated version for large lists
            return show_paginated_link_selection(field_name, field_label, link_doctype, data, missing_fields, user, current_doctype, 1)
        
        # Use original logic for smaller lists
        records = frappe.get_all(link_doctype, 
                                fields=["name"],
                                order_by="name",
                                limit=20)  # Limit for better performance
        
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
                                   order_by="name",
                                   limit=20)
        
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
        
        record_names = []
        
        if not records:
            return f"""{icon} Select {field_label}

No {link_doctype.lower()}s found.

You can:
• Type a name directly
• Type 'cancel' to cancel"""
        
        record_names = [record.name for record in records]
        
        # Unicode circled numbers for beautiful badges (purple theme)
        circled_numbers = ["①", "②", "③", "④", "⑤", "⑥", "⑦", "⑧", "⑨", "⑩", "⑪", "⑫", "⑬", "⑭", "⑮", "⑯", "⑰", "⑱", "⑲", "⑳"]
        
        # Create beautiful response with heavy markdown styling
        response_parts = [
            f"{icon} **Select {field_label}**",
            f"*Choose from {len(records)} available {link_doctype.lower()}s*\n"
        ]
        
        # Add the beautiful option cards with circled numbers
        response_parts.append(f"**📋 Available {link_doctype}s:**")
        for i, record in enumerate(records, 1):
            display_name = record.name
            if display_field and record.get(display_field) and record.get(display_field) != record.name:
                display_name += f" *({record.get(display_field)})*"
            badge = circled_numbers[i-1] if i <= len(circled_numbers) else f"({i})"
            response_parts.append(f"{badge} **{display_name}**")
        
        response_parts.extend([
            "",
            "**💡 How to select:**",
            "• Type a **number** (e.g., `3`) for your choice",
            "• Type the **{} name** directly".format(link_doctype.lower()),
            "• Type `cancel` to cancel operation",
            "",
            "**📝 Quick Examples:**",
            f"• `1` → Select **{records[0].name}**" if records else "",
            f"• `{records[0].name}` → Select by exact name" if records else "",
            "• `cancel` → Cancel this operation",
            "",
            f"**🎯 {link_doctype} Selection Details:**",
            f"• **Field:** {field_label}",
            f"• **Type:** {link_doctype} Link",
            f"• **Available:** {len(records)} {link_doctype.lower()}s",
            f"• **Search:** Type any name for direct selection"
        ])
        
        response_text = "\n".join([part for part in response_parts if part])
        
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
        
        return response_text
        
    except Exception as e:
        return f"Error showing {field_label} selection: {str(e)}"

def show_paginated_link_selection(field_name, field_label, link_doctype, data, missing_fields, user, current_doctype, page=1):
    """Show paginated link field selection with beautiful HTML interface"""
    try:
        # Try to get a better display field
        link_meta = frappe.get_meta(link_doctype)
        display_field = None
        for field in ["title", "full_name", "employee_name", "customer_name", "supplier_name", "item_name", "currency_name"]:
            if link_meta.get_field(field):
                display_field = field
                break
        
        # Get all records for this doctype
        if display_field:
            all_records = frappe.get_all(link_doctype, 
                                       fields=["name", display_field],
                                       order_by="name")
        else:
            all_records = frappe.get_all(link_doctype, 
                                       fields=["name"],
                                       order_by="name")
        
        # Pagination settings
        items_per_page = 15  # Reduced for better display
        total_items = len(all_records)
        total_pages = (total_items + items_per_page - 1) // items_per_page
        
        # Calculate start and end indices for current page
        start_idx = (page - 1) * items_per_page
        end_idx = min(start_idx + items_per_page, total_items)
        current_page_records = all_records[start_idx:end_idx]
        
        record_names = [record.name for record in current_page_records]
        
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
        
        if not all_records:
            return f"""{icon} Select {field_label}

No {link_doctype.lower()}s found.

You can:
• Type a {link_doctype.lower()} name directly
• Type 'cancel' to cancel"""
        
        # Unicode circled numbers for beautiful badges (purple theme)
        circled_numbers = ["①", "②", "③", "④", "⑤", "⑥", "⑦", "⑧", "⑨", "⑩", "⑪", "⑫", "⑬", "⑭", "⑮", "⑯", "⑰", "⑱", "⑲", "⑳"]
        
        # Create beautiful response with heavy markdown styling
        response_parts = [
            f"{icon} **Select {field_label}**",
            f"*Page {page} of {total_pages} • {total_items} total {link_doctype.lower()}s available*\n"
        ]
        
        # Add the beautiful option cards with circled numbers
        response_parts.append(f"**📋 Available {link_doctype}s (Page {page}):**")
        for i, record in enumerate(current_page_records, 1):
            display_name = record.name
            if display_field and record.get(display_field) and record.get(display_field) != record.name:
                display_name += f" *({record.get(display_field)})*"
            badge = circled_numbers[i-1] if i <= len(circled_numbers) else f"({i})"
            response_parts.append(f"{badge} **{display_name}**")
        
        # Add navigation info if multiple pages with beautiful styling
        if total_pages > 1:
            nav_info = []
            if page > 1:
                nav_info.append("`prev` ← Previous page")
            if page < total_pages:
                nav_info.append("`next` → Next page")
            
            if nav_info:
                response_parts.extend([
                    "",
                    f"**🔄 Page Navigation:** {' | '.join(nav_info)}"
                ])
        
        response_parts.extend([
            "",
            "**💡 How to select:**",
            "• Type a **number** (e.g., `3`) from the list above",
            f"• Type the **{link_doctype.lower()} name** directly",
            "• Type `cancel` to cancel operation"
        ])
        
        if total_pages > 1:
            response_parts.extend([
                "",
                "**📖 Navigation Commands:**",
                "• `next` → Go to next page of results",
                "• `prev` → Go to previous page of results"
            ])
        
        response_parts.extend([
            "",
            "**📝 Quick Examples:**",
            f"• `1` → Select first {link_doctype.lower()} from current page",
            f"• `{current_page_records[0].name}` → Direct selection by name" if current_page_records else "",
            "• `cancel` → Cancel this operation",
            "",
            f"**🎯 {link_doctype} Selection Details:**",
            f"• **Field:** {field_label}",
            f"• **Current Page:** {page} of {total_pages}",
            f"• **Total Available:** {total_items} {link_doctype.lower()}s",
            f"• **Per Page:** {items_per_page} items",
            f"• **Search:** Type any name for instant match"
        ])
        
        response_text = "\n".join([part for part in response_parts if part])
        
        # Save state with pagination info
        state = {
            "action": "collect_stock_selection",
            "selection_type": field_name,
            "doctype": current_doctype,
            "data": data,
            "missing_fields": missing_fields,
            "numbered_options": record_names,
            "pagination": {
                "current_page": page,
                "total_pages": total_pages,
                "items_per_page": items_per_page,
                "total_items": total_items
            }
        }
        set_conversation_state(user, state)
        
        return response_text
        
    except Exception as e:
        return f"Error showing {field_label} selection: {str(e)}"

def show_generic_select_selection(field_name, field_label, options, data, missing_fields, user, current_doctype):
    """Show beautiful selection for any Select field with heavy markdown styling"""
    try:
        # Parse options (they come as newline-separated string)
        option_list = [opt.strip() for opt in options.split('\n') if opt.strip()]
        
        # Remove empty first option if present
        if option_list and option_list[0] == '':
            option_list = option_list[1:]
        
        if not option_list:
            return f"""📝 **Select {field_label}**

**ℹ️ No Options Available**

**💡 What you can do:**
• Type `cancel` to cancel this operation
• Contact your **administrator** to configure field options
• Check if this field should have predefined values

**🔧 Field Information:**
• **Field:** {field_label}
• **Type:** Select (Dropdown)
• **Status:** No options configured"""
        
        # Unicode circled numbers for beautiful badges (purple theme)
        circled_numbers = ["①", "②", "③", "④", "⑤", "⑥", "⑦", "⑧", "⑨", "⑩", "⑪", "⑫", "⑬", "⑭", "⑮", "⑯", "⑰", "⑱", "⑲", "⑳"]
        
        # Create beautiful response with sections
        response_parts = [
            f"📝 **Select {field_label}**",
            f"*Choose from {len(option_list)} available options*\n"
        ]
        
        # Add the beautiful option cards with circled numbers
        response_parts.append("**⚙️ Available Options:**")
        for i, option in enumerate(option_list, 1):
            badge = circled_numbers[i-1] if i <= len(circled_numbers) else f"({i})"
            response_parts.append(f"{badge} **{option}**")
        
        response_parts.extend([
            "",
            "**💡 How to select:**",
            "• Type a **number** (e.g., `3`) for your choice",
            "• Type the **option name** directly",
            "• Type `cancel` to cancel operation",
            "",
            "**📝 Quick Examples:**",
            f"• `1` → Select **{option_list[0]}**" if option_list else "",
            f"• `{option_list[0]}` → Select by name" if option_list else "",
            "• `cancel` → Cancel this operation",
            "",
            f"**🎯 Field Details:**",
            f"• **Field:** {field_label}",
            f"• **Options:** {len(option_list)} available",
            f"• **Type:** Select (Dropdown)",
            f"• **Required:** {'Yes' if field_name in missing_fields else 'Optional'}"
        ])
        
        response_text = "\n".join([part for part in response_parts if part])  # Remove empty parts
        
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
        
        return response_text
        
    except Exception as e:
        return f"Error showing {field_label} selection: {str(e)}"

def show_generic_currency_selection(field_name, field_label, data, missing_fields, user, current_doctype):
    """Show beautiful currency input interface with Markdown formatting"""
    try:
        # Create examples
        examples = ["50000", "25000.50", "100.99"]
        
        # Create beautiful response with heavy markdown styling
        response_parts = [
            f"💰 **Enter {field_label}**",
            f"*Input a currency amount for your {field_label.lower()}*\n"
        ]
        
        # Add beautiful examples section
        response_parts.extend([
            "**📝 Amount Examples:**",
            f"• `{examples[0]}` → ₹{examples[0]} (Perfect format)",
            f"• `{examples[1]}` → ₹{examples[1]} (With decimals)",
            f"• `{examples[2]}` → ₹{examples[2]} (Small amount)",
            ""
        ])
        
        response_parts.extend([
            "**💰 Currency Amount Guidelines:**",
            "• **Whole amounts:** `1000`, `50000`, `100000`",
            "• **Decimal amounts:** `1000.50`, `25000.75`, `99.99`",
            "• **Large amounts:** `1000000` (1 million), `5000000` (5 million)",
            "• **Zero amount:** `0` if no value required",
            "",
            "**✅ Valid Format Examples:**",
            "• `50000` → Fifty thousand",
            "• `25000.50` → Twenty-five thousand and fifty cents",
            "• `100.99` → One hundred and ninety-nine cents",
            "• `1000000` → One million",
            "",
            "**❌ Invalid Formats:**",
            "• ~~`₹50000`~~ (No currency symbol needed)",
            "• ~~`50,000`~~ (No commas allowed)",
            "• ~~`50k`~~ (No abbreviations)",
            "",
            "**💡 How to enter:**",
            "• Type the **amount as a number** directly",
            "• Use **decimal point** for cents (e.g., `25000.50`)",
            "• Type `0` if **no amount** or zero value",
            "• Type `cancel` to cancel operation",
            "",
            "**🚀 Pro Tips:**",
            "• **Precision:** Use up to 2 decimal places for cents",
            "• **Large amounts:** System handles millions/billions",
            "• **Auto-conversion:** System converts to proper currency format",
            "• **Validation:** Invalid amounts will be rejected with guidance",
            "",
            f"**🎯 Amount Input Details:**",
            f"• **Field:** {field_label}",
            f"• **Type:** Currency Amount (Number)",
            f"• **Format:** Decimal number (no symbols)",
            f"• **Range:** 0 to 999,999,999,999.99",
            f"• **Status:** Required monetary input"
        ])
        
        response_text = "\n".join([part for part in response_parts if part])
        
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
        
        return response_text
        
    except Exception as e:
        return f"Error showing {field_label} input: {str(e)}"

def show_currency_link_selection(field_name, field_label, data, missing_fields, user, current_doctype, page=1):
    """Show paginated currency selection with beautiful HTML interface"""
    try:
        # Get available currencies
        all_currencies = frappe.get_all("Currency", 
                                      fields=["name", "currency_name", "symbol"],
                                      order_by="name")
        
        # Pagination settings
        items_per_page = 15  # Reduced for better display
        total_items = len(all_currencies)
        total_pages = (total_items + items_per_page - 1) // items_per_page
        
        # Calculate start and end indices for current page
        start_idx = (page - 1) * items_per_page
        end_idx = min(start_idx + items_per_page, total_items)
        current_page_currencies = all_currencies[start_idx:end_idx]
        
        currency_names = [curr.name for curr in current_page_currencies]
        
        if not all_currencies:
            return """💱 **Select Currency**

ℹ️ No currencies found.

**💡 How to proceed:**
• Type a **currency code** directly (e.g., USD, EUR)
• Type `cancel` to cancel

**📋 Field:** Currency | **🔍 Status:** No currencies found"""
        
        # Unicode circled numbers for beautiful badges (purple theme)
        circled_numbers = ["①", "②", "③", "④", "⑤", "⑥", "⑦", "⑧", "⑨", "⑩", "⑪", "⑫", "⑬", "⑭", "⑮", "⑯", "⑰", "⑱", "⑲", "⑳"]
        
        # Create beautiful Markdown interface with heavy styling
        popular_currencies = ["USD", "EUR", "GBP", "INR", "JPY", "CAD", "AUD", "SGD"]
        popular_found = [curr for curr in all_currencies if curr.name in popular_currencies]
        
        response_parts = [
            f"💱 **Select {field_label}**",
            f"*Page {page} of {total_pages} • {total_items} total currencies available*\n"
        ]
        
        # Add popular currencies section if available with beautiful formatting
        if popular_found:
            response_parts.append("**⭐ Popular Currencies:**")
            for i, curr in enumerate(popular_found[:6], 1):  # Show top 6 popular currencies
                symbol_text = f" `{curr.symbol}`" if curr.symbol else ""
                badge = circled_numbers[i-1] if i <= len(circled_numbers) else f"({i})"
                response_parts.append(f"{badge} **{curr.name}**{symbol_text}")
            response_parts.append("")
        
        # Add currencies for current page with beautiful cards
        response_parts.append(f"**💰 All Currencies (Page {page}/{total_pages}):**")
        start_num = len(popular_found[:6]) + 1 if popular_found else 1
        for i, currency in enumerate(current_page_currencies, start_num):
            currency_display = currency.name
            if currency.currency_name and currency.currency_name != currency.name:
                currency_display += f" *({currency.currency_name})*"
            if currency.symbol:
                currency_display += f" `{currency.symbol}`"
            badge = circled_numbers[i-1] if i <= len(circled_numbers) else f"({i})"
            response_parts.append(f"{badge} **{currency_display}**")
        
        # Add navigation info if multiple pages
        if total_pages > 1:
            nav_info = []
            if page > 1:
                nav_info.append("`prev` ← Previous page")
            if page < total_pages:
                nav_info.append("`next` → Next page")
            
            if nav_info:
                response_parts.extend([
                    "",
                    f"**🔄 Page Navigation:** {' | '.join(nav_info)}"
                ])
        
        response_parts.extend([
            "",
            "**💡 How to select:**",
            "• Type a **number** (e.g., `3`) from the options above",
            "• Type the **currency code** directly (e.g., `USD`, `INR`)",
            "• Type a **popular currency** from the ⭐ section",
            "• Type `cancel` to cancel operation"
        ])
        
        if total_pages > 1:
            response_parts.extend([
                "",
                "**📖 Navigation Commands:**",
                "• `next` → Go to next page of currencies",
                "• `prev` → Go to previous page of currencies"
            ])
        
        response_parts.extend([
            "",
            "**📝 Quick Examples:**",
            "• `1` → Select first currency from list",
            "• `USD` → US Dollar (direct search)",
            "• `INR` → Indian Rupee (direct search)",
            "• `EUR` → Euro (direct search)",
            "",
            f"**🎯 Currency Selection Details:**",
            f"• **Field:** {field_label}",
            f"• **Current Page:** {page} of {total_pages}",
            f"• **Total Available:** {total_items} currencies",
            f"• **Search:** Type any currency code for instant match"
        ])
        
        response_text = "\n".join(response_parts)
        
        # Save state with pagination info and all currency names for search
        all_currency_names = [curr.name for curr in all_currencies]
        state = {
            "action": "collect_stock_selection",
            "selection_type": field_name,
            "doctype": current_doctype,
            "data": data,
            "missing_fields": missing_fields,
            "numbered_options": currency_names,  # Current page options
            "all_currency_options": all_currency_names,  # All currencies for direct search
            "pagination": {
                "current_page": page,
                "total_pages": total_pages,
                "items_per_page": items_per_page,
                "total_items": total_items
            }
        }
        set_conversation_state(user, state)
        
        return response_text
        
    except Exception as e:
        return f"Error showing {field_label} selection: {str(e)}"

def show_generic_numeric_selection(field_name, field_label, fieldtype, data, missing_fields, user, current_doctype):
    """Show simple numeric input interface"""
    try:
        # Create appropriate icon and examples based on field type
        if fieldtype == "Int":
            icon = "🔢"
            examples = ["100", "250", "1000"]
            description = f"whole number"
        elif fieldtype == "Percent":
            icon = "📊" 
            examples = ["15", "25.5", "100"]
            description = f"percentage (0-100)"
        else:  # Float
            icon = "💯"
            examples = ["100.50", "25.75", "1000.99"]
            description = f"decimal number"
        
        # Create beautiful response with heavy markdown styling
        response_parts = [
            f"{icon} **Enter {field_label}**",
            f"*Input a {description} for this field*\n"
        ]
        
        # Add beautiful examples section
        response_parts.extend([
            "**📝 Input Examples:**",
            f"• `{examples[0]}` → Perfect format",
            f"• `{examples[1]}` → Another example", 
            f"• `{examples[2]}` → Large number format",
            ""
        ])
        
        # Add detailed instructions
        if fieldtype == "Int":
            response_parts.extend([
                "**🔢 Integer Number Guidelines:**",
                "• **Whole numbers only:** `100`, `2500`, `10000`",
                "• **No decimals allowed:** ❌ `100.5` ✅ `100`",
                "• **Positive numbers preferred:** `1` to `999999999`",
                "• **Zero allowed:** `0` for no value"
            ])
        elif fieldtype == "Percent":
            response_parts.extend([
                "**📊 Percentage Guidelines:**",
                "• **Range:** `0` to `100` percent",
                "• **Decimals allowed:** `15.5`, `25.75`, `100.00`",
                "• **Whole percentages:** `15`, `50`, `100`",
                "• **Common values:** `10`, `15`, `18`, `25`"
            ])
        else:  # Float
            response_parts.extend([
                "**💯 Decimal Number Guidelines:**",
                "• **Decimal format:** `100.50`, `25.75`, `1000.99`",
                "• **Whole numbers:** `100`, `250`, `1000`",
                "• **Scientific notation:** `1e3` (equals 1000)",
                "• **High precision:** `123.456789`"
            ])
        
        response_parts.extend([
            "",
            "**💡 How to enter:**",
            f"• Type a {description} directly",
            "• Type `0` if no value or zero amount",
            "• Type `cancel` to cancel operation",
            "",
            f"**🎯 Field Information:**",
            f"• **Field:** {field_label}",
            f"• **Type:** {fieldtype} (Number)",
            f"• **Format:** {description.title()}",
            f"• **Status:** Required input"
        ])
        
        response_text = "\n".join([part for part in response_parts if part])
        
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
        
        return response_text
        
    except Exception as e:
        return f"Error showing {field_label} input: {str(e)}"

def show_generic_date_selection(field_name, field_label, data, missing_fields, user, current_doctype):
    """Show simple date selection interface"""
    try:
        from datetime import date, timedelta
        
        today = date.today()
        tomorrow = today + timedelta(days=1)
        week_later = today + timedelta(days=7)
        month_later = today + timedelta(days=30)
        
        date_options = [
            today.strftime("%Y-%m-%d"),
            tomorrow.strftime("%Y-%m-%d"), 
            week_later.strftime("%Y-%m-%d"),
            month_later.strftime("%Y-%m-%d")
        ]
        
        # Get current year for examples
        current_year = today.year
        
        # Unicode circled numbers for beautiful badges (purple theme)
        circled_numbers = ["①", "②", "③", "④", "⑤", "⑥", "⑦", "⑧", "⑨", "⑩"]
        
        # Create beautiful response with heavy markdown styling
        response_parts = [
            f"📅 **Select {field_label}**",
            f"*Choose a date for your {field_label.lower()}*\n"
        ]
        
        # Add beautiful quick date options with circled numbers
        response_parts.extend([
            "**⚡ Quick Date Options:**",
            f"{circled_numbers[0]} **Today** - `{today.strftime('%Y-%m-%d')}` ({today.strftime('%A')})",
            f"{circled_numbers[1]} **Tomorrow** - `{tomorrow.strftime('%Y-%m-%d')}` ({tomorrow.strftime('%A')})",
            f"{circled_numbers[2]} **Next Week** - `{week_later.strftime('%Y-%m-%d')}` ({week_later.strftime('%A')})",
            f"{circled_numbers[3]} **Next Month** - `{month_later.strftime('%Y-%m-%d')}` ({month_later.strftime('%A')})",
            ""
        ])
        
        response_parts.extend([
            "**💡 How to select:**",
            "• Type a **number** (e.g., `2`) for quick date options",
            "• Type a **custom date** in `YYYY-MM-DD` format",
            "• Type `cancel` to cancel operation",
            "",
            "**📝 Custom Date Examples:**",
            f"• `{current_year}-12-25` → Christmas {current_year}",
            f"• `{current_year+1}-06-15` → Mid-year {current_year+1}",
            f"• `{current_year+1}-03-01` → March 1st {current_year+1}",
            "",
            "**📋 Date Format Guidelines:**",
            "• **Required format:** `YYYY-MM-DD` (4-digit year)",
            "• **Valid examples:** `2024-12-31`, `2025-01-15`",
            "• **Invalid examples:** ❌ `31/12/2024` ❌ `Dec 31 2024`",
            "",
            f"**🎯 Date Selection Details:**",
            f"• **Field:** {field_label}",
            f"• **Today's Date:** {today.strftime('%Y-%m-%d')} ({today.strftime('%A')})",
            f"• **Format Required:** YYYY-MM-DD",
            f"• **Quick Options:** 4 available above"
        ])
        
        response_text = "\n".join([part for part in response_parts if part])
        
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
        
        return response_text
        
    except Exception as e:
        return f"Error showing {field_label} selection: {str(e)}"

def show_generic_text_input(field_name, field_label, data, missing_fields, user, current_doctype):
    """Show simple text input interface"""
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
        
        # Create context-specific examples and instructions
        if "email" in field_name.lower():
            examples = ["john.doe@company.com", "admin@example.org"]
        elif "phone" in field_name.lower() or "mobile" in field_name.lower():
            examples = ["+91 9876543210", "9876543210"]
        elif "name" in field_name.lower():
            if current_doctype == "User":
                examples = ["John Doe", "Mary Johnson"]
            elif current_doctype in ["Customer", "Supplier"]:
                examples = ["ABC Corporation", "XYZ Suppliers Ltd"]
            else:
                examples = ["John Doe", "ABC Corporation"]
        elif "address" in field_name.lower():
            examples = ["123 Main Street, City, State", "Building A, Tech Park, Bangalore"]
        elif "website" in field_name.lower():
            examples = ["https://www.company.com", "www.example.org"]
        else:
            examples = [f"Your {field_label.lower()} here"]
        
        # Create beautiful response with heavy markdown styling
        response_parts = [
            f"{icon} **Enter {field_label}**",
            f"*Input text for your {field_label.lower()}*\n"
        ]
        
        # Add beautiful examples section
        response_parts.extend([
            "**📝 Input Examples:**",
            f"• `{examples[0]}` → Perfect format",
            f"• `{examples[1] if len(examples) > 1 else examples[0]}` → Alternative example",
            ""
        ])
        
        # Add specific guidelines based on field type
        if "email" in field_name.lower():
            response_parts.extend([
                "**📧 Email Guidelines:**",
                "• **Format:** `username@domain.com`",
                "• **Valid examples:** `john@company.com`, `admin@website.org`",
                "• **Required parts:** Username + @ + Domain",
                "• **Case:** Usually lowercase preferred"
            ])
        elif "phone" in field_name.lower() or "mobile" in field_name.lower():
            response_parts.extend([
                "**📱 Phone Guidelines:**",
                "• **With country code:** `+91 9876543210`",
                "• **Without code:** `9876543210`",
                "• **Format options:** Numbers with/without spaces",
                "• **Length:** Usually 10+ digits"
            ])
        elif "name" in field_name.lower():
            response_parts.extend([
                "**👤 Name Guidelines:**",
                "• **Person names:** `John Doe`, `Mary Johnson`",
                "• **Company names:** `ABC Corporation`, `XYZ Ltd`",
                "• **Format:** Proper capitalization preferred",
                "• **Length:** 2-100 characters typical"
            ])
        elif "address" in field_name.lower():
            response_parts.extend([
                "**📍 Address Guidelines:**",
                "• **Complete format:** `Street, City, State, Country`",
                "• **Example:** `123 Main St, New York, NY, USA`",
                "• **Include:** Building/Street + City + State/Region",
                "• **Postal code:** Include if available"
            ])
        elif "website" in field_name.lower():
            response_parts.extend([
                "**🌐 Website Guidelines:**",
                "• **Full URL:** `https://www.company.com`",
                "• **Simple format:** `www.company.com`",
                "• **Protocol:** http:// or https:// preferred",
                "• **Valid domains:** .com, .org, .net, etc."
            ])
        else:
            response_parts.extend([
                "**✏️ Text Input Guidelines:**",
                "• **Free form text:** Type any relevant text",
                "• **Length:** Keep reasonable length",
                "• **Special chars:** Most characters allowed",
                "• **Format:** No specific format required"
            ])
        
        response_parts.extend([
            "",
            "**💡 How to enter:**",
            "• Type your text **directly** in the chat",
            "• Press **Enter** to submit your input",
            "• Type `cancel` to cancel operation",
            "",
            f"**🎯 Field Information:**",
            f"• **Field:** {field_label}",
            f"• **Type:** Text Input",
            f"• **Icon:** {icon}",
            f"• **Status:** Required text input"
        ])
        
        response_text = "\n".join([part for part in response_parts if part])
        
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
        
        return response_text
        
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
            elif link_doctype == "Currency":
                return show_currency_link_selection(field_name, field_label, data, missing_fields, user, current_doctype)
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
        
        elif fieldtype == "Dynamic Link":
            # Handle Dynamic Link fields (like Payment Entry party field)
            # Dynamic Link uses another field to determine target doctype
            if current_doctype == "Payment Entry" and field_name == "party":
                # Use party_type to determine which doctype to show
                party_type = data.get("party_type")
                if party_type == "Customer":
                    return show_generic_link_selection(field_name, field_label, "Customer", data, missing_fields, user, current_doctype)
                elif party_type == "Supplier":
                    return show_generic_link_selection(field_name, field_label, "Supplier", data, missing_fields, user, current_doctype)
                elif party_type == "Employee":
                    return show_generic_link_selection(field_name, field_label, "Employee", data, missing_fields, user, current_doctype)
                else:
                    # Fallback if party_type not set - shouldn't happen with auto-setting
                    return show_generic_text_input(field_name, field_label, data, missing_fields, user, current_doctype)
            else:
                # Generic Dynamic Link handling - fallback to text input
                return show_generic_text_input(field_name, field_label, data, missing_fields, user, current_doctype)
        
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

# show_transaction_items_selection and related transaction functions removed - replaced by generic child table system

def handle_child_table_field_input(message, state, user):
    """Handle input for child table fields with enhanced numbered options"""
    try:
        user_input = message.strip()
        
        # Handle cancel at any stage
        if user_input.lower() in ['cancel', 'quit', 'exit']:
            clear_conversation_state(user)
            doctype = state.get("doctype", "Document")
            return f"{doctype} creation cancelled."
        
        # Get the child table state and field info
        child_table_data = state.get("child_table_data", {})
        field_info = state.get("field_info", {})
        numbered_options = state.get("numbered_options", [])
        
        fieldname = field_info["fieldname"]
        fieldtype = field_info["fieldtype"]
        field_label = field_info["label"]
        
        selected_value = None
        
        # Handle numbered options first (for Link, Select, Date fields)
        if numbered_options and user_input.isdigit():
                try:
                    num = int(user_input)
                    if 1 <= num <= len(numbered_options):
                      selected_value = numbered_options[num - 1]
                    else:
                        return f"❌ Invalid number: {num}. Please use numbers between 1 and {len(numbered_options)}."
                except ValueError:
                        return f"❌ Invalid input. Please use numbers or direct input."
        else:
            # Handle direct input or non-numeric fields
            try:
                # Use the existing validation function
                selected_value = validate_field_input(user_input, field_info)
            except ValueError as e:
                # Show error and ask again with appropriate interface
                return f"❌ {str(e)}\n\nPlease try again or type `cancel` to cancel."
        
        # If we got a valid value, update the child table data
        if selected_value is not None:
            current_row = child_table_data.get("current_row", {})
            current_row[fieldname] = selected_value
            
            # Move to next field
            current_field_index = child_table_data.get("current_field_index", 0)
            child_table_data["current_row"] = current_row
            child_table_data["current_field_index"] = current_field_index + 1
            
            # Update the original child table state
            state["child_table_data"] = child_table_data
            
            # Continue with the original child table collection flow
            child_table_data["stage"] = "collect_field"
            set_conversation_state(user, child_table_data)
            
            return start_child_field_collection(child_table_data, user)
        else:
            return "❌ Invalid input. Please try again or type `cancel` to cancel."
            
    except Exception as e:
        clear_conversation_state(user)
        return f"Error processing child table field input: {str(e)}"

@frappe.whitelist()
def clear_user_conversation_state(user_email=None):
    """Clear conversation state for a user (for debugging)"""
    if not user_email:
        user_email = frappe.session.user
    
    clear_conversation_state(user_email)
    return f"Conversation state cleared for {user_email}"