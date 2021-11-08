// ==UserScript==
// @name       Asana page id helpers
// @namespace  https://github.com/aspiers/desktop-config/tree/master/lib/browser/userscripts/page-id-helpers
// @version    0.1.0
// @description Functions to return identifiers for Asana
// @match      https://app.asana.com/*
// @author     Adam Spiers
// @grant      none
// @downloadURL https://github.com/aspiers/desktop-config/raw/master/lib/browser/userscripts/page-id-helpers/Asana%20page%20id%20helpers.user.js
// ==/UserScript==

window.asana_extract_title = function () {
    var m = document.title.match(/^(.+) - (.+) - Asana$/);
    if (m) {
        return("Asana: " + m[2]);
    }
    return(document.title);
};

window.page_id = window.asana_extract_title;

window.page_title = window.asana_extract_title;
