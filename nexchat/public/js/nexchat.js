class NexchatWidget {
    constructor() {
        this.isOpen = false;
        this.isTyping = false;
        this.cleanup(); // Clean up any existing widgets first
        this.init();
    }

    cleanup() {
        // Remove any existing widgets to prevent duplicates
        $('.nexchat-widget, .nexchat-toggle').remove();
        // Clear the global reference
        if (window.nexchat && window.nexchat !== this) {
            window.nexchat = null;
        }
    }

    init() {
        this.createToggleButton();
        this.createChatWidget();
        this.bindEvents();
        this.addWelcomeMessage();
    }

    createToggleButton() {
        const toggleButton = $(`
            <button class="nexchat-toggle" title="Open Nexchat Assistant">
                💬
            </button>
        `);
        
        $('body').append(toggleButton);
        
        toggleButton.on('click', () => {
            this.toggleWidget();
        });
    }

    createChatWidget() {
        const chatWidget = $(`
            <div class="nexchat-widget hidden">
                <div class="nexchat-header">
                    <span>Nexchat</span>
                    <button class="nexchat-close" title="Close chat">×</button>
                </div>
                <div class="nexchat-body" id="nexchat-body">
                    <div class="nexchat-typing" id="nexchat-typing">Nexchat is thinking</div>
                </div>
                <div class="nexchat-footer">
                    <div class="nexchat-input-container">
                        <input type="text" id="nexchat-input" placeholder="Ask me anything about ERPNext..." autocomplete="off">
                        <button class="nexchat-send-btn" id="nexchat-send" title="Send message">
                            <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
                                <path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/>
                            </svg>
                        </button>
                    </div>
                </div>
            </div>
        `);

        $('body').append(chatWidget);
    }

    bindEvents() {
        // Close button
        $('.nexchat-close').on('click', () => {
            this.toggleWidget();
        });

        // Send button
        $('#nexchat-send').on('click', () => {
            this.handleUserInput();
        });

        // Enter key in input
        $('#nexchat-input').on('keypress', (e) => {
            if (e.which === 13 && !e.shiftKey) {
                e.preventDefault();
                this.handleUserInput();
            }
        });

        // Auto-resize and focus management
        $('#nexchat-input').on('focus', () => {
            setTimeout(() => {
                this.scrollToBottom();
            }, 100);
        });
    }

    toggleWidget() {
        const widget = $('.nexchat-widget');
        const toggle = $('.nexchat-toggle');
        
        if (this.isOpen) {
            widget.addClass('hidden');
            toggle.show();
            this.isOpen = false;
        } else {
            widget.removeClass('hidden');
            toggle.hide();
            this.isOpen = true;
            setTimeout(() => {
                $('#nexchat-input').focus();
                this.scrollToBottom();
            }, 100);
        }
    }

    destroy() {
        // Clean up event listeners and DOM elements
        $(document).off('keydown.nexchat-nav');
        $('.nexchat-widget, .nexchat-toggle').remove();
        window.nexchat = null;
    }

    addWelcomeMessage() {
        const welcomeMessages = [
            "Hello! I'm Nexchat, your ERPNext AI assistant. How can I help you today?",
            "I can help you create documents, find information, and navigate ERPNext. What would you like to do?",
            "✨ Try asking me: 'Create a new customer' or 'Show me my sales orders'"
        ];
        
        welcomeMessages.forEach((message, index) => {
            setTimeout(() => {
                this.addMessage(message, 'bot', false);
            }, index * 1000);
        });
    }

    addMessage(text, sender, animate = true) {
        const messageClass = sender === 'user' ? 'user' : 'bot';
        
        // Check message type for special formatting
        const isRoleSelection = text.includes('Select Role(s) for') && text.includes('`');
        const isHTMLResponse = text.includes('<div class="nexchat-') || text.includes('<div class="nexchat-options-container">') || text.includes('<div class="nexchat-field-container">');
        
        let messageContent;
        if (isRoleSelection && sender === 'bot') {
            messageContent = this.formatRoleSelectionMessage(text);
        } else if (isHTMLResponse && sender === 'bot') {
            // Use HTML response directly
            messageContent = text;
        } else {
            messageContent = `<p>${this.escapeHtml(text)}</p>`;
        }
        
        const message = $(`
            <div class="nexchat-message ${messageClass} ${isRoleSelection ? 'role-selection' : ''} ${isHTMLResponse ? 'html-response' : ''}" style="${animate ? 'opacity: 0; transform: translateY(10px);' : ''}">
                ${messageContent}
            </div>
        `);

        $('#nexchat-body').append(message);

        // Add click handlers for interactive elements
        if (isRoleSelection) {
            this.addRoleButtonHandlers();
        }
        
        if (isHTMLResponse) {
            this.addOptionHandlers();
        }

        if (animate) {
            setTimeout(() => {
                message.css({
                    'opacity': '1',
                    'transform': 'translateY(0)',
                    'transition': 'all 0.3s ease'
                });
            }, 50);
        }

        this.scrollToBottom();
    }

    formatRoleSelectionMessage(text) {
        // Convert backtick-wrapped numbers to clickable buttons
        let formattedText = text.replace(/`(\d+)`\s+\*\*(.*?)\*\*/g, (match, number, roleName) => {
            return `<button class="role-button" data-number="${number}" onclick="window.nexchat.selectRole('${number}')">${number}</button> <strong>${roleName}</strong>`;
        });
        
        // Convert other markdown formatting
        formattedText = formattedText.replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>');
        formattedText = formattedText.replace(/`(.*?)`/g, '<code>$1</code>');
        
        // Convert line breaks to HTML
        formattedText = formattedText.replace(/\n/g, '<br>');
        
        // Add special action buttons at the end if this is role selection
        const actionButtons = `
            <div class="role-action-buttons">
                <button class="assign-all-button" onclick="window.nexchat.assignAllRoles()" title="Assign ALL available roles at once">
                    🚀 Assign ALL Roles
                </button>
                <button class="cancel-button" onclick="window.nexchat.selectRole('cancel')" title="Cancel role assignment">
                    ❌ Cancel
                </button>
            </div>
        `;
        
        return `<div class="role-selection-content">${formattedText}${actionButtons}</div>`;
    }

    addRoleButtonHandlers() {
        // Additional setup for role selection if needed
        $('.role-button').off('click').on('click', function(e) {
            e.preventDefault();
            const number = $(this).data('number');
            window.nexchat.selectRole(number);
        });
    }

    selectRole(number) {
        // Auto-fill the input with the selected number
        const input = $('#nexchat-input');
        const currentValue = input.val().trim();
        
        if (currentValue === '') {
            input.val(number);
        } else if (currentValue.includes(',') || /^\d+$/.test(currentValue)) {
            // If already has numbers, append with comma
            input.val(currentValue + ',' + number);
        } else {
            // Replace with new number
            input.val(number);
        }
        
        input.focus();
    }

    assignAllRoles() {
        // Auto-fill the input with the "all roles" command
        const input = $('#nexchat-input');
        input.val('all roles');
        
        // Automatically send the message
        this.handleUserInput();
    }

    addOptionHandlers() {
        // Handle option selection clicks
        $('.nexchat-option-item').off('click').on('click', function(e) {
            e.preventDefault();
            const value = $(this).data('value') || $(this).find('.nexchat-option-primary').text().trim();
            window.nexchat.selectOption(value);
        });

        // Handle collapsible section toggles
        $('.nexchat-collapsible-header').off('click').on('click', function(e) {
            e.preventDefault();
            window.nexchat.toggleCollapsible(this);
        });

        // Handle search input
        $('.nexchat-search-input').off('input').on('input', function(e) {
            window.nexchat.filterOptions($(this).val());
        });

        // Add keyboard navigation
        this.addKeyboardNavigation();
    }

    selectOption(value) {
        // Handle special pagination commands
        if (value === 'next_page' || value === 'prev_page') {
            value = value.replace('_', ' '); // Convert to 'next page' or 'prev page'
        }
        
        // Auto-fill the input with the selected value
        const input = $('#nexchat-input');
        input.val(value);
        input.focus();
        
        // Optionally auto-submit for better UX
        setTimeout(() => {
            this.handleUserInput();
        }, 100);
    }

    toggleCollapsible(header) {
        const section = $(header).closest('.nexchat-collapsible-section');
        section.toggleClass('expanded');
        
        // Update icon rotation
        const icon = section.find('.nexchat-collapsible-icon');
        if (section.hasClass('expanded')) {
            icon.css('transform', 'rotate(180deg)');
        } else {
            icon.css('transform', 'rotate(0deg)');
        }
    }

    filterOptions(searchValue) {
        const searchLower = searchValue.toLowerCase();
        $('.nexchat-option-item').each(function() {
            const primaryText = $(this).find('.nexchat-option-primary').text().toLowerCase();
            const secondaryText = $(this).find('.nexchat-option-secondary').text().toLowerCase();
            
            if (primaryText.includes(searchLower) || secondaryText.includes(searchLower)) {
                $(this).show();
            } else {
                $(this).hide();
            }
        });

        // Auto-expand collapsible sections when searching
        if (searchValue.trim()) {
            $('.nexchat-collapsible-section').addClass('expanded');
        }
    }

    addKeyboardNavigation() {
        // Remove existing handlers to prevent duplicates
        $(document).off('keydown.nexchat-nav');
        
        $(document).on('keydown.nexchat-nav', (e) => {
            // Only handle if chat widget is open and there are visible options
            if (!this.isOpen) return;
            
            const visibleOptions = $('.nexchat-option-item:visible');
            if (visibleOptions.length === 0) return;
            
            if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
                e.preventDefault();
                
                const currentFocus = $('.nexchat-option-item.focused');
                let currentIndex = currentFocus.length ? visibleOptions.index(currentFocus) : -1;
                
                // Remove current focus
                currentFocus.removeClass('focused');
                
                // Calculate new index
                if (e.key === 'ArrowDown') {
                    currentIndex = (currentIndex + 1) % visibleOptions.length;
                } else {
                    currentIndex = currentIndex <= 0 ? visibleOptions.length - 1 : currentIndex - 1;
                }
                
                // Add focus to new option
                const newFocused = visibleOptions.eq(currentIndex);
                newFocused.addClass('focused');
                
                // Scroll into view
                newFocused[0].scrollIntoView({ 
                    behavior: 'smooth', 
                    block: 'nearest' 
                });
                
            } else if (e.key === 'Enter') {
                e.preventDefault();
                const focusedOption = $('.nexchat-option-item.focused');
                if (focusedOption.length) {
                    focusedOption.click();
                }
            } else if (e.key === 'Escape') {
                // Clear focus on escape
                $('.nexchat-option-item.focused').removeClass('focused');
            }
        });
    }

    showTyping() {
        this.isTyping = true;
        $('#nexchat-typing').addClass('show');
        this.scrollToBottom();
    }

    hideTyping() {
        this.isTyping = false;
        $('#nexchat-typing').removeClass('show');
    }

    handleUserInput() {
        const input = $('#nexchat-input');
        const sendBtn = $('#nexchat-send');
        const userInput = input.val().trim();

        if (!userInput || this.isTyping) return;

        // Add user message
        this.addMessage(userInput, 'user');
        input.val('');

        // Disable input while processing
        input.prop('disabled', true);
        sendBtn.prop('disabled', true);
        this.showTyping();

        // Send to backend
        this.sendToBackend(userInput)
            .then(response => {
                this.hideTyping();
                if (response && response.response) {
                    this.addMessage(response.response, 'bot');
                } else {
                    this.addMessage('Sorry, I encountered an error processing your request. Please try again.', 'bot');
                }
            })
            .catch(error => {
                this.hideTyping();
                console.error('Nexchat Error:', error);
                this.addMessage('I apologize, but I\'m having trouble connecting right now. Please check your connection and try again.', 'bot');
            })
            .finally(() => {
                // Re-enable input
                input.prop('disabled', false);
                sendBtn.prop('disabled', false);
                input.focus();
            });
    }

    sendToBackend(message) {
        return new Promise((resolve, reject) => {
            frappe.call({
                method: 'nexchat.api.process_message',
                args: {
                    message: message
                },
                callback: function(r) {
                    if (r.message) {
                        resolve(r.message);
                    } else {
                        reject(new Error('No response from server'));
                    }
                },
                error: function(r) {
                    reject(new Error(r.responseJSON ? r.responseJSON.message : 'Server error'));
                }
            });
        });
    }

    scrollToBottom() {
        const chatBody = $('#nexchat-body');
        chatBody.scrollTop(chatBody[0].scrollHeight);
    }

    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }
}

// Initialize the chat widget when the page loads
$(document).ready(function() {
    initializeNexchat();
});

// Handle page navigation in single-page app
$(document).on('page-change', function() {
    initializeNexchat();
});

function initializeNexchat() {
    // Only initialize if user is logged in and widget doesn't already exist
    if (frappe.session && frappe.session.user && frappe.session.user !== 'Guest') {
        // Check if widget already exists to prevent duplicates
        if (!window.nexchat && $('.nexchat-widget, .nexchat-toggle').length === 0) {
            window.nexchat = new NexchatWidget();
        }
    }
} 