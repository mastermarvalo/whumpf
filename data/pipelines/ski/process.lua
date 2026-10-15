-- tilemaker profile: ski runs, lifts and ski areas from OSM.
--
-- Layers and fields are the contract with the apps; change them together with
-- EXPECTED_FIELDS in data/pipelines/ski_tiles.py and the runbook.
--
--   ski_runs   piste_type, difficulty, grooming, name, osm_id, osm_type
--   ski_lifts  lift_type, name, osm_id, osm_type
--   ski_areas  name, osm_id, osm_type  (Polygon outline + Point label)

-- Only nodes with these keys are kept as features; we emit none.
node_keys = {}

local PISTE_TYPES = { downhill = true, nordic = true, skitour = true }

local DIFFICULTIES = {
  novice = true, easy = true, intermediate = true, advanced = true,
  expert = true, freeride = true, extreme = true,
}

-- Passenger lifts only: no goods, zip_line, pylon or station.
local LIFT_TYPES = {
  cable_car = true, gondola = true, mixed_lift = true, chair_lift = true,
  drag_lift = true, ["t-bar"] = true, ["j-bar"] = true, platter = true,
  rope_tow = true, magic_carpet = true,
}

local function set_string(key, value)
  if value ~= nil and value ~= "" then Attribute(key, string.sub(value, 1, 128)) end
end

-- osm_id is a string: tilemaker writes numbers as float32, which corrupts
-- any id above 2^24 (e.g. way 1349624424 came out as 1349624400).
local function set_id(osm_type)
  Attribute("osm_id", Id())
  Attribute("osm_type", osm_type)
end

-- Route relations (route=piste) carry the tags for many nordic and skitour
-- trails whose member ways have none of their own.
function relation_scan_function()
  if Find("type") == "route" and Find("route") == "piste" then
    local piste_type = Find("piste:type")
    if PISTE_TYPES[piste_type] then Accept() end
  end
end

function relation_function()
end

-- First piste route this way belongs to, as a table of its tags.
local function piste_route()
  while true do
    local rel = NextRelation()
    if not rel then return nil end
    local piste_type = FindInRelation("piste:type")
    if PISTE_TYPES[piste_type] then
      return {
        piste_type = piste_type,
        difficulty = FindInRelation("piste:difficulty"),
        grooming = FindInRelation("piste:grooming"),
        name = FindInRelation("piste:name") ~= "" and FindInRelation("piste:name") or FindInRelation("name"),
      }
    end
  end
end

local function emit_run(is_relation)
  local piste_type = Find("piste:type")
  local route = nil
  if not is_relation then route = piste_route() end
  if not PISTE_TYPES[piste_type] then
    if not route then return end
    piste_type = route.piste_type
  end
  route = route or {}

  local difficulty = Find("piste:difficulty")
  if difficulty == "" then difficulty = route.difficulty or "" end
  local grooming = Find("piste:grooming")
  if grooming == "" then grooming = route.grooming or "" end
  local name = Find("piste:name")
  if name == "" then name = Find("name") end
  if name == "" then name = route.name or "" end

  -- Runs drawn as areas (area=yes, or multipolygon relations) stay polygons;
  -- every other way is a line, including closed loops of nordic trail.
  local as_area = is_relation or (IsClosed() and Find("area") == "yes")
  Layer("ski_runs", as_area)
  Attribute("piste_type", piste_type)
  if DIFFICULTIES[difficulty] then Attribute("difficulty", difficulty) end
  -- "classic;skating" and "classic+skating" both occur; the wiki value is "+".
  set_string("grooming", (string.gsub(grooming, ";", "+")))
  set_string("name", name)
  set_id(is_relation and "relation" or "way")
end

local function emit_lift()
  local lift_type = Find("aerialway")
  if not LIFT_TYPES[lift_type] then return end
  Layer("ski_lifts", false)
  Attribute("lift_type", lift_type)
  set_string("name", Find("name"))
  set_id("way")
end

local function emit_area(is_relation)
  if Find("landuse") ~= "winter_sports" then return end
  if not (is_relation or IsClosed()) then return end
  local osm_type = is_relation and "relation" or "way"
  Layer("ski_areas", true)
  set_string("name", Find("name"))
  set_id(osm_type)
  LayerAsCentroid("ski_areas")
  set_string("name", Find("name"))
  set_id(osm_type)
end

-- Called for every way and for every multipolygon relation.
function way_function()
  local is_relation = IsMultiPolygon()
  emit_run(is_relation)
  if not is_relation then emit_lift() end
  emit_area(is_relation)
end
