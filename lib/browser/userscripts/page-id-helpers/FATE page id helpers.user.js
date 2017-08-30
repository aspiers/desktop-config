// ==UserScript==
// @name       FATE page id helpers
// @namespace  https://github.com/aspiers/desktop-config/tree/master/lib/browser/userscripts/page-id-helpers
// @version    0.1.4
// @description Functions to return identifiers for FATE pages
// @match      https://fate.suse.com/*
// @copyright  Adam Spiers
// @grant      none
// @downloadURL https://github.com/aspiers/desktop-config/raw/master/lib/browser/userscripts/page-id-helpers/FATE%20page%20id%20helpers.user.js
// ==/UserScript==

fate_extract_metadata = function () {
    var m = document.title.match(/^webFATE - #(\d+): (.+)$/);
    if (m) {
        return({
            number: m[1],
            title: m[2],
        });
    }
    return null;
};

page_id = function () {
    var m = fate_extract_metadata();
    if (m) {
        return("FATE#" + m.number);
    }
    return(null);
};

page_title = function () {
    var m = fate_extract_metadata();
    if (m) {
        return("FATE#" + m.number + " (" + m.title + ")");
    }
    return(document.title);
};
