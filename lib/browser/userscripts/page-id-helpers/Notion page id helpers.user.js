// ==UserScript==
// @name       Notion page id helpers
// @namespace  https://github.com/aspiers/desktop-config/tree/master/lib/browser/userscripts/page-id-helpers
// @version    0.1.0
// @description Functions to return identifiers for Notion pages
// @match      https://notion.so/*
// @match      https://www.notion.so/*
// @author     Adam Spiers
// @grant      none
// @downloadURL https://github.com/aspiers/desktop-config/raw/master/lib/browser/userscripts/page-id-helpers/Notion%20page%20id%20helpers.user.js
// ==/UserScript==

window.notion_extract_title = function () {
    // Trim out the "(9+)" kind of prefix which denotes the number
    // of unread notifications.
    var m = document.title.match(/^\(\d+\+?\) (.+)$/);
    if (m) {
        return(m[1]);
    }
    return(document.title);
};

window.page_id = window.notion_extract_title;

window.page_title = window.notion_extract_title;
