-- Prevent Bluetooth earbud/headphone loopback mics from becoming the
-- default source.  PipeWire's bluez monitor creates loopback source nodes
-- (bluez5.loopback=true) with a hardcoded priority of 2010, and since
-- those nodes have object.register=false, no rules mechanism can override
-- it.  This hook runs after find-best-default-node and, if the winner is
-- a loopback source, re-selects the best non-loopback node instead.

log = Log.open_topic ("s-default-nodes")

nutils = require ("node-utils")

SimpleEventHook {
  name = "default-nodes/demote-bt-loopback-source",
  after = { "default-nodes/find-best-default-node" },
  before = { "default-nodes/find-selected-default-node",
             "default-nodes/find-stored-default-node",
             "default-nodes/apply-default-node" },
  interests = {
    EventInterest {
      Constraint { "event.type", "=", "select-default-node" },
    },
  },
  execute = function (event)
    local props = event:get_properties ()
    local def_node_type = props ["default-node.type"]

    -- Only act on source selection
    if def_node_type ~= "audio.source" then
      return
    end

    local available_nodes = event:get_data ("available-nodes")
    local selected_node = event:get_data ("selected-node")

    available_nodes = available_nodes and available_nodes:parse ()
    if not available_nodes or not selected_node then
      return
    end

    -- Check whether the selected node is a BT loopback source
    local selected_is_loopback = false
    for _, node_props in ipairs (available_nodes) do
      if node_props ["node.name"] == selected_node then
        if node_props ["bluez5.loopback"] == "true" or
           node_props ["bluez5.loopback"] == true then
          selected_is_loopback = true
        end
        break
      end
    end

    if not selected_is_loopback then
      return
    end

    -- Re-select the highest-priority non-loopback node
    local best_prio = -1
    local best_node = nil

    for _, node_props in ipairs (available_nodes) do
      local is_loopback = node_props ["bluez5.loopback"] == "true"
                       or node_props ["bluez5.loopback"] == true
      if not is_loopback then
        local priority = nutils.get_session_priority (node_props)
        if priority > best_prio then
          best_prio = priority
          best_node = node_props ["node.name"]
        end
      end
    end

    if best_node then
      log:info ("demoting BT loopback source " .. selected_node
                .. " in favour of " .. best_node)
      event:set_data ("selected-node-priority", best_prio)
      event:set_data ("selected-node", best_node)
    end
  end
}:register ()
