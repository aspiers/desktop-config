// ==UserScript==
// @name       Linear page id helpers
// @namespace  https://github.com/aspiers/desktop-config/tree/master/lib/browser/userscripts/page-id-helpers
// @version    0.1.0
// @description Functions to return identifiers for Linear
// @match      https://linear.app/*
// @author     Adam Spiers
// @grant      none
// @downloadURL https://github.com/aspiers/desktop-config/raw/master/lib/browser/userscripts/page-id-helpers/Linear%20page%20id%20helpers.user.js
// ==/UserScript==

window.linear_extract_metadata = function () {
    var m = document.title.match(/^([A-Z]\w+-\d+)\s+(.+?)\s*$/);
    if (m) {
        return({
            id: m[1],
            title: m[2]
        });
    }
    return null;
};

window.page_id = function () {
    var m = window.linear_extract_metadata();
    if (m) {
        return(m.id);
    }
    return(null);
};

window.page_title = function () {
    var m = window.linear_extract_metadata();
    if (m) {
        return(m.id + " (" + m.title + ")");
    }
    return(document.title);
};
