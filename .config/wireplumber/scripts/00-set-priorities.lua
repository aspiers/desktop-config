#!/usr/bin/env lua
-- WirePlumber Lua script to set audio device priorities and auto-switch streams
-- Priorities: USB audio (1000) > Bluetooth (900) > HDMI (800) > built-in (700)
-- Automatically switches to highest priority available device on connect/disconnect

local function matches_patterns(text, patterns)
    if not text then return false end
    text = text:lower()
    for _, pattern in ipairs(patterns) do
        if text:find(pattern) then
            return true
        end
    end
    return false
end

local function get_priority_for_device(name, desc)
    local combined = (name or "") .. " " .. (desc or "")

    if matches_patterns(combined, {
        "usb", "kt_usb", "audioengine", "fiio", "schiit",
        "chord", "apogee", "focusrite", "motu", "gomic"
    }) then
        return 1000, "USB audio"
    end

    if matches_patterns(combined, {
        "wh-", "sony", "headphones", "airpods", "bose",
        "skullcandy", "jabra", "xm[0-9]", "wh1000", "wh-1000"
    }) then
        return 900, "Bluetooth"
    end

    if matches_patterns(combined, {"hdmi", "displayport", "dp-"}) then
        return 800, "HDMI/DisplayPort"
    end

    return 700, "built-in"
end

local function set_node_priority(node)
    local name = node.properties["node.name"] or ""
    local desc = node.properties["node.description"] or ""
    local priority, category = get_priority_for_device(name, desc)

    node.properties["priority.session"] = priority
    print(string.format("[priority] %s (%s) -> priority.session=%d", name, desc, priority))
end

local function get_best_device(device_type)
    local objects = core:get_objects(device_type)
    local best = nil
    local best_priority = -1

    if not objects then return nil, -1 end

    for _, node in ipairs(objects) do
        local name = node.properties["node.name"] or ""
        local desc = node.properties["node.description"] or ""
        local priority, _ = get_priority_for_device(name, desc)

        if priority > best_priority then
            best_priority = priority
            best = node
        end
    end

    return best, best_priority
end

local function notify(title, message)
    os.execute(string.format("notify-send '%s' '%s' 2>/dev/null", title, message))
end

local function notify_switch(device_type, old_name, new_name)
    if old_name and new_name and old_name ~= new_name then
        notify("Audio Switched", string.format("%s: %s → %s", device_type, old_name, new_name))
    elseif new_name then
        notify("Audio Selected", string.format("%s: %s", device_type, new_name))
    end
end

local function switch_stream_to_node(stream, target_node)
    if not target_node then return end

    local stream_name = stream.properties["media.name"] or stream.properties["node.name"] or "unknown"
    local target_id = target_node.properties["object.id"]
    local target_name = target_node.properties["node.name"] or "unknown"

    print(string.format("[switch] Moving stream '%s' to %s (id=%d)",
        stream_name, target_name, target_id))

    core:set_default_node(target_node)
end

local function reevaluate_and_switch()
    local old_sink = get_best_device("Audio/Sink")
    print("[event] Re-evaluating audio device selection...")

    local best_sink, sink_priority = get_best_device("Audio/Sink")
    local best_source, source_priority = get_best_device("Audio/Source")

    if best_sink then
        local sink_name = best_sink.properties["node.name"] or "unknown"
        print(string.format("[event] Best sink: %s (priority=%d)", sink_name, sink_priority))
    end

    if best_source then
        local source_name = best_source.properties["node.name"] or "unknown"
        print(string.format("[event] Best source: %s (priority=%d)", source_name, source_priority))
    end

    local streams = core:get_objects("Stream/Output/Audio")
    if streams and #streams > 0 then
        print(string.format("[event] Switching %d active streams...", #streams))
        for _, stream in ipairs(streams) do
            switch_stream_to_node(stream, best_sink)
        end
    end

    if best_sink then
        notify_switch("Sink", nil, best_sink.properties["node.name"])
    end
    if best_source then
        notify_switch("Source", nil, best_source.properties["node.name"])
    end
end

local function main()
    print("========================================")
    print("WirePlumber Audio Priority Script")
    print("========================================")

    print("[init] Setting priorities on existing devices...")

    local sinks = core:get_objects("Audio/Sink")
    if sinks and #sinks > 0 then
        print(string.format("[init] Processing %d sinks...", #sinks))
        for _, node in ipairs(sinks) do
            set_node_priority(node)
        end
    end

    local sources = core:get_objects("Audio/Source")
    if sources and #sources > 0 then
        print(string.format("[init] Processing %d sources...", #sources))
        for _, node in ipairs(sources) do
            set_node_priority(node)
        end
    end

    print("[init] Priority configuration complete.")
    print("")
    print("[init] WirePlumber will now automatically:")
    print("  - Use highest priority device for new streams")
    print("  - Switch existing streams on device changes")
    print("========================================")
end

main()

-- Hook into device changes for automatic switching
core:connect("device-added", function(device)
    local device_name = device.properties["device.name"] or device.properties["device.description"] or "unknown"
    print(string.format("[hook] Device added: %s", device_name))
    notify("Audio Device Connected", device_name)
    reevaluate_and_switch()
end)

core:connect("device-removed", function(device)
    local device_name = device.properties["device.name"] or device.properties["device.description"] or "unknown"
    print(string.format("[hook] Device removed: %s", device_name))
    notify("Audio Device Disconnected", device_name)
    reevaluate_and_switch()
end)
