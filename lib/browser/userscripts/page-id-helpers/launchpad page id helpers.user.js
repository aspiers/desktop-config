// ==UserScript==
// @name       launchpad page id helpers
// @namespace  https://github.com/aspiers/desktop-config/tree/master/lib/browser/userscripts/page-id-helpers
// @version    0.1.1
// @description Functions to return identifiers for launchpad pages
// @match      https://bugs.launchpad.net/*
// @author     Adam Spiers
// @grant      none
// @downloadURL https://github.com/aspiers/desktop-config/raw/master/lib/browser/userscripts/page-id-helpers/launchpad%20page%20id%20helpers.user.js
// ==/UserScript==

launchpad_extract_title = function () {
    var m = document.title.match(/^Bug #(\d+) “(.+)” : Bugs : (.+)$/);
    if (m) {
        return({
            number: m[1],
            title: m[2],
            component: m[3]
        });
    }
    return null;
};

page_id = function () {
    var m = launchpad_extract_title();
    if (m) {
        return("lp#" + m.number);
    }
    return(null);
};

page_title = function () {
    var m = launchpad_extract_title();
    if (m) {
        return("lp#" + m.number + " (" + m.component + "): " + m.title);
    }
    return(document.title);
};
