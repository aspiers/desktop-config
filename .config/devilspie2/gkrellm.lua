if (get_window_name() == "gkrellm") then
    pin_window();
    set_skip_tasklist(true);
    set_window_below();
    undecorate_window();
end
