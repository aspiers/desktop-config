if (string.match(get_window_name(), "^watchlogs.*")) then
    pin_window();
    set_skip_tasklist(true);
    set_window_below();
    undecorate_window();
end
