// ==UserScript==
// @name       etherpad page id helpers
// @namespace  https://github.com/aspiers/desktop-config/tree/master/lib/browser/userscripts/page-id-helpers
// @version    0.1.2
// @description Functions to return identifiers for SUSE etherpads
// @match      https://etherpad.nue.suse.com/*
// @author     Adam Spiers
// @grant      none
// @downloadURL https://github.com/aspiers/desktop-config/raw/master/lib/browser/userscripts/page-id-helpers/etherpad%20page%20id%20helpers.user.js
// ==/UserScript==

etherpad_extract_title = function () {
    var m = document.title.match(/^(.+) \| SUSE OPS-Services Etherpad$/);
    if (m) {
        return(m[1] + " etherpad");
    }
    return(document.title);
};

page_id = etherpad_extract_title;

page_title = etherpad_extract_title;
