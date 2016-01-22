// ==UserScript==
// @name       Trello page id helpers
// @namespace  https://github.com/aspiers/desktop-config/tree/master/lib/browser/userscripts/page-id-helpers
// @version    0.1.3
// @description Functions to return identifiers for Trello cards
// @match      https://trello.com/*
// @author     Adam Spiers
// @grant      none
// @downloadURL https://github.com/aspiers/desktop-config/raw/master/lib/browser/userscripts/page-id-helpers/Trello%20page%20id%20helpers.user.js
// ==/UserScript==

trello_extract_title = function () {
    var m = document.title.match(/^(?:\((\d+)\) )?(.+) on (.+) | Trello$/);
    if (m) {
        return(m[2] + " (Trello)");
    }
    return(document.title);
};

page_id = trello_extract_title;

page_title = trello_extract_title;
