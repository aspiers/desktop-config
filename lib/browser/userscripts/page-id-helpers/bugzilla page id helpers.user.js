// ==UserScript==
// @name       bugzilla page id helpers
// @namespace  https://github.com/aspiers/desktop-config/tree/master/lib/browser/userscripts/page-id-helpers
// @version    0.1.3
// @description Functions to return identifiers for bugzilla pages
// @match      https://bugzilla.suse.com/*
// @author     Adam Spiers
// @grant      none
// @downloadURL https://github.com/aspiers/desktop-config/raw/master/lib/browser/userscripts/page-id-helpers/bugzilla%20page%20id%20helpers.user.js
// ==/UserScript==

bsc_extract_title = function () {
    var m = document.title.match(/^Bug (\d+) – (.+)$/);
    if (m) {
        return({
            number: m[1],
            title: m[2],
        });
    }
    return null;
};

page_id = function () {
    var m = bsc_extract_title();
    if (m) {
        return("bsc#" + m.number);
    }
    return(null);
};

page_title = function () {
    var m = bsc_extract_title();
    if (m) {
        return("bsc#" + m.number + " (" + m.title + ")");
    }
    return(document.title);
};
