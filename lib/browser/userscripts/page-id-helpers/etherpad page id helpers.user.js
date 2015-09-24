// ==UserScript==
// @name       etherpad page id helpers
// @namespace  http://adamspiers.org/
// @version    0.1
// @description Functions to return identifiers for SUSE etherpads
// @match      https://etherpad.nue.suse.com/*
// @author     2015 Adam Spiers
// @grant      none
// ==/UserScript==

etherpad_extract_title = function () {
    var m = document.title.match(/^(.+) \| SUSE OPS-Services Etherpad$/);
    if (m) {
        return(m[1] + " etherpad");
    }
    return null;
};

page_id = etherpad_extract_title;

page_title = etherpad_extract_title;
