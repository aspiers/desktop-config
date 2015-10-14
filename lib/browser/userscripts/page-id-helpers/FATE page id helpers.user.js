// ==UserScript==
// @name       FATE page id helpers
// @namespace  http://adamspiers.org/
// @version    0.1
// @description Functions to return identifiers for FATE pages
// @match      https://fate.suse.com/*
// @copyright  2015 Adam Spiers
// @grant      none
// ==/UserScript==

bsc_extract_metadata = function () {
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
    var m = bsc_extract_metadata();
    if (m) {
        return("FATE#" + m.number);
    }
    return(null);
};

page_title = function () {
    var m = bsc_extract_metadata();
    if (m) {
        return("FATE#" + m.number + ": " + m.title);
    }
    return(document.title);
};