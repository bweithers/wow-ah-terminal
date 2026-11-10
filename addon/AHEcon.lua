-- AHEcon: export item details and auction order-book depth for desktop analysis.
--
-- Two jobs:
--
--  1. Item details. The desktop tool writes ItemIDs.lua listing every item ID
--     it found in auction data. On login this addon resolves each one to a
--     name, vendor sell price, stack size, quality and item level, asking the
--     server for anything not in the local cache. No auction house visit is
--     needed.
--
--  2. Order-book depth. While the auction house is open, per-item search
--     results carry the full price ladder (every listing, not just the
--     cheapest). Auctionator keeps only a daily minimum, so this records the
--     ladder for any item whose results come past -- which happens as a side
--     effect of normal browsing and selling.
--
-- Everything is additive and read-only: no other addon is touched, nothing is
-- bought, sold, posted or cancelled, and no query is sent that the player did
-- not already trigger.

local PER_TICK = 40        -- item IDs resolved per frame
local MAX_PASSES = 8       -- retries while the server fills the item cache
local LADDER_KEEP = 12     -- cheapest N listings recorded per item
local DEBUG_KEEP = 60      -- event trace entries kept

AHECON_NAMES = AHECON_NAMES or {}   -- [itemID] = name
AHECON_ITEMS = AHECON_ITEMS or {}   -- [itemID] = {n,s,st,q,il,c,sc}
AHECON_DEPTH = AHECON_DEPTH or {}   -- [itemID] = {t,tq,na,p={{u,q},...}}
AHECON_META = AHECON_META or {}
AHECON_DEBUG = AHECON_DEBUG or {}

local queue, qi, pass, resolved = {}, 1, 0, 0
local depthSeen = 0

local function trace(event, detail)
    AHECON_DEBUG[#AHECON_DEBUG + 1] = {
        t = time(), ev = event, d = detail,
    }
    while #AHECON_DEBUG > DEBUG_KEEP do
        table.remove(AHECON_DEBUG, 1)
    end
end

-- --------------------------------------------------------------------- --
-- item details
-- --------------------------------------------------------------------- --

local function itemInfo(itemID)
    local get = (C_Item and C_Item.GetItemInfo) or GetItemInfo
    -- GetItemInfo returns, in order: name, link, quality, itemLevel, minLevel,
    -- type, subType, stackCount, equipLoc, texture, sellPrice, classID,
    -- subClassID, ... (pcall prepends its own success flag)
    local ok, name, _, quality, itemLevel, _, _, _, stack, _, _, sellPrice,
          classID, subClassID = pcall(get, itemID)
    if ok and name and name ~= "" then
        return {
            n = name,
            s = sellPrice,
            st = stack,
            q = quality,
            il = itemLevel,
            c = classID,
            sc = subClassID,
        }
    end
    return nil
end

local function request(itemID)
    if C_Item and C_Item.RequestLoadItemDataByID then
        pcall(C_Item.RequestLoadItemDataByID, itemID)
    end
end

-- Collect every wanted ID that is still unresolved.
local function buildQueue()
    queue, qi = {}, 1
    local wanted = AHECON_WANTED
    if type(wanted) ~= "table" then
        trace("queue", "no ItemIDs.lua")
        return 0
    end
    for i = 1, #wanted do
        local id = wanted[i]
        if type(id) == "number" and not AHECON_ITEMS[id] then
            queue[#queue + 1] = id
        end
    end
    return #queue
end

local function record(itemID, info)
    AHECON_ITEMS[itemID] = info
    AHECON_NAMES[itemID] = info.n
    resolved = resolved + 1
end

local driver = CreateFrame("Frame")
local elapsed = 0

driver:SetScript("OnUpdate", function(_, delta)
    -- End of a pass: anything the server had not sent yet gets another go.
    if qi > #queue then
        if pass >= MAX_PASSES then
            trace("done", resolved .. " resolved, " .. #queue .. " unresolved")
            driver:Hide()
            return
        end
        pass = pass + 1
        local left = buildQueue()
        if left == 0 then
            trace("done", resolved .. " resolved, all known")
            driver:Hide()
        else
            trace("retry", "pass " .. pass .. ", " .. left .. " left")
        end
        return
    end

    elapsed = elapsed + delta
    if elapsed < 0.1 then return end
    elapsed = 0

    local stop = math.min(qi + PER_TICK - 1, #queue)
    for i = qi, stop do
        local id = queue[i]
        local info = itemInfo(id)
        if info then
            record(id, info)
        else
            request(id)
        end
    end
    qi = stop + 1
end)

-- --------------------------------------------------------------------- --
-- order-book depth
-- --------------------------------------------------------------------- --

local function saveLadder(itemID, ladder, totalQty, numAuctions)
    if not itemID or #ladder == 0 then return end
    table.sort(ladder, function(a, b) return a.u < b.u end)
    local keep = {}
    for i = 1, math.min(LADDER_KEEP, #ladder) do
        keep[i] = { u = ladder[i].u, q = ladder[i].q }
    end
    AHECON_DEPTH[itemID] = {
        t = time(), tq = totalQty, na = numAuctions, p = keep,
    }
    depthSeen = depthSeen + 1
end

local function captureCommodity(itemID)
    if not (C_AuctionHouse and C_AuctionHouse.GetNumCommoditySearchResults) then
        return
    end
    local ok, n = pcall(C_AuctionHouse.GetNumCommoditySearchResults, itemID)
    if not ok or not n or n == 0 then return end
    local ladder, total, auctions = {}, 0, 0
    for i = 1, n do
        local ok2, r = pcall(C_AuctionHouse.GetCommoditySearchResultInfo, itemID, i)
        if ok2 and r and r.unitPrice then
            ladder[#ladder + 1] = { u = r.unitPrice, q = r.quantity or 0 }
            total = total + (r.quantity or 0)
            auctions = auctions + (r.numAuctions or 1)
        end
    end
    saveLadder(itemID, ladder, total, auctions)
    trace("commodity", itemID .. " x" .. #ladder)
end

-- Browse results are what a full scan produces: one aggregate row per item
-- (cheapest price, total quantity) rather than a full ladder. Cheap to record
-- and it arrives for every item in the scan.
local function captureBrowse()
    if not (C_AuctionHouse and C_AuctionHouse.GetNumBrowseResults) then return end
    local ok, n = pcall(C_AuctionHouse.GetNumBrowseResults)
    if not ok or not n or n == 0 then return end
    local seen = 0
    for i = 1, n do
        local ok2, r = pcall(C_AuctionHouse.GetBrowseResultInfo, i)
        local id = ok2 and r and r.itemKey and r.itemKey.itemID
        if id and r.minPrice then
            local prev = AHECON_DEPTH[id]
            -- never overwrite a real ladder with a single aggregate row
            if not (prev and prev.p and #prev.p > 1 and prev.t == time()) then
                AHECON_DEPTH[id] = {
                    t = time(),
                    tq = r.totalQuantity,
                    na = r.containsOwnerItem and -1 or nil,
                    p = { { u = r.minPrice, q = r.totalQuantity or 0 } },
                    agg = true,
                }
            end
            seen = seen + 1
        end
    end
    depthSeen = depthSeen + seen
    trace("browse", n .. " rows, " .. seen .. " recorded")
end

local function captureItem(itemKey)
    if not (C_AuctionHouse and C_AuctionHouse.GetNumItemSearchResults) then return end
    if type(itemKey) ~= "table" or not itemKey.itemID then return end
    local ok, n = pcall(C_AuctionHouse.GetNumItemSearchResults, itemKey)
    if not ok or not n or n == 0 then return end
    local ladder, total = {}, 0
    for i = 1, n do
        local ok2, r = pcall(C_AuctionHouse.GetItemSearchResultInfo, itemKey, i)
        if ok2 and r then
            local qty = r.quantity or 1
            local unit = r.buyoutAmount
            if unit and unit > 0 then
                ladder[#ladder + 1] = { u = math.floor(unit / qty), q = qty }
                total = total + qty
            end
        end
    end
    saveLadder(itemKey.itemID, ladder, total, n)
    trace("itemsearch", itemKey.itemID .. " x" .. #ladder)
end

-- A full "replicate" scan returns every individual auction at once, which is
-- the richest source of depth. Available only when the client offers it.
local function captureReplicate()
    if not (C_AuctionHouse and C_AuctionHouse.GetNumReplicaItems) then return end
    local ok, n = pcall(C_AuctionHouse.GetNumReplicaItems)
    if not ok or not n or n == 0 then return end
    local byItem = {}
    for i = 0, n - 1 do
        local ok2, info = pcall(function()
            return { C_AuctionHouse.GetReplicateItemInfo(i) }
        end)
        if ok2 and info then
            local name, count, buyout, itemID =
                info[1], info[3], info[10], info[17]
            if itemID and buyout and buyout > 0 then
                local qty = (count and count > 0) and count or 1
                byItem[itemID] = byItem[itemID] or { ladder = {}, total = 0 }
                local b = byItem[itemID]
                b.ladder[#b.ladder + 1] = { u = math.floor(buyout / qty), q = qty }
                b.total = b.total + qty
                if name and not AHECON_NAMES[itemID] then
                    AHECON_NAMES[itemID] = name
                end
            end
        end
    end
    local seen = 0
    for itemID, b in pairs(byItem) do
        saveLadder(itemID, b.ladder, b.total, #b.ladder)
        seen = seen + 1
    end
    trace("replicate", n .. " auctions, " .. seen .. " items")
end

-- --------------------------------------------------------------------- --
-- events
-- --------------------------------------------------------------------- --

local function stamp()
    AHECON_META.lastRun = time()
    AHECON_META.realm = GetRealmName and GetRealmName() or "?"
    AHECON_META.itemCount = 0
    for _ in pairs(AHECON_ITEMS) do
        AHECON_META.itemCount = AHECON_META.itemCount + 1
    end
    AHECON_META.depthCount = 0
    for _ in pairs(AHECON_DEPTH) do
        AHECON_META.depthCount = AHECON_META.depthCount + 1
    end
    AHECON_META.wanted = type(AHECON_WANTED) == "table" and #AHECON_WANTED or 0
end

local f = CreateFrame("Frame")
for _, ev in ipairs({
    "PLAYER_LOGIN",
    "PLAYER_LOGOUT",
    "GET_ITEM_INFO_RECEIVED",
    "COMMODITY_SEARCH_RESULTS_UPDATED",
    "ITEM_SEARCH_RESULTS_UPDATED",
    "REPLICATE_ITEM_LIST_UPDATE",
    "AUCTION_HOUSE_BROWSE_RESULTS_UPDATED",
    "AUCTION_HOUSE_BROWSE_RESULTS_ADDED",
    "AUCTION_HOUSE_SHOW",
}) do
    pcall(f.RegisterEvent, f, ev)
end

f:SetScript("OnEvent", function(_, event, arg1)
    if event == "PLAYER_LOGIN" then
        pass = 0
        local left = buildQueue()
        driver:Show()
        trace("login", left .. " to resolve")
    elseif event == "GET_ITEM_INFO_RECEIVED" then
        if arg1 and AHECON_WANTED and not AHECON_ITEMS[arg1] then
            local info = itemInfo(arg1)
            if info then record(arg1, info) end
        end
    elseif event == "COMMODITY_SEARCH_RESULTS_UPDATED" then
        captureCommodity(arg1)
    elseif event == "ITEM_SEARCH_RESULTS_UPDATED" then
        captureItem(arg1)
    elseif event == "REPLICATE_ITEM_LIST_UPDATE" then
        captureReplicate()
    elseif event == "AUCTION_HOUSE_BROWSE_RESULTS_UPDATED"
        or event == "AUCTION_HOUSE_BROWSE_RESULTS_ADDED" then
        captureBrowse()
    elseif event == "AUCTION_HOUSE_SHOW" then
        -- record which depth APIs this client actually offers, so a missing
        -- order book can be told apart from a missing event
        local have = {}
        if C_AuctionHouse then
            for _, api in ipairs({ "GetNumBrowseResults", "GetNumReplicaItems",
                                   "GetNumCommoditySearchResults",
                                   "GetNumItemSearchResults", "ReplicateItems" }) do
                if C_AuctionHouse[api] then have[#have + 1] = api end
            end
        end
        trace("ah_open", table.concat(have, ","))
    elseif event == "PLAYER_LOGOUT" then
        stamp()
        trace("logout", resolved .. " resolved, " .. depthSeen .. " ladders")
    end
end)

-- --------------------------------------------------------------------- --
-- slash command
-- --------------------------------------------------------------------- --

SLASH_AHECON1 = "/ahecon"
SlashCmdList["AHECON"] = function(msg)
    msg = (msg or ""):lower():gsub("^%s+", ""):gsub("%s+$", "")
    local function say(s) print("|cff44ff44AHEcon|r: " .. s) end

    if msg == "clear" then
        AHECON_NAMES, AHECON_ITEMS, AHECON_DEPTH, AHECON_DEBUG = {}, {}, {}, {}
        resolved, depthSeen = 0, 0
        say("cleared. /reload to write the empty file, then re-run.")
        return
    end

    if msg == "scan" or msg == "names" then
        pass = 0
        local left = buildQueue()
        driver:Show()
        say("resolving " .. left .. " item IDs; run /ahecon again in a moment.")
        return
    end

    stamp()
    local wanted = AHECON_META.wanted or 0
    say(("%d of %d items resolved, %d depth ladders recorded.")
        :format(AHECON_META.itemCount or 0, wanted, AHECON_META.depthCount or 0))
    if wanted == 0 then
        say("no ItemIDs.lua found -- run 'python ah.py sync-ids' on the desktop.")
    elseif (AHECON_META.itemCount or 0) < wanted then
        say("still working; /ahecon scan forces another pass.")
    end
    say("/reload (or log out) to write the file, then 'python ah.py refresh'.")
end
