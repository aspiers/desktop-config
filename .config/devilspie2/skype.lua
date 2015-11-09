-- Call window
if (string.find(get_window_name(), "Call Phones or Send SMS") and
    get_window_class() == "Skype") then
    pin_window();
end

-- Call window
if (string.match(get_window_name(), "Call with") and
    (get_window_role() == "CallWindowForm" or get_window_class() == "Skype")) then
    pin_window();
end

-- Chat window
if (string.find(get_window_name(), "Skype") and
    string.match(get_window_role(), "Chats|ConversationsWindow")) then
    pin_window();
end
