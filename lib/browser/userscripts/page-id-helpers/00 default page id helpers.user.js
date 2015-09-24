// ==UserScript==
// @name       default page id helpers
// @namespace  http://adamspiers.org/
// @version    0.1
// @description Functions to return default identifiers for all pages
// @match      *://*/*
// @author     2015 Adam Spiers
// @grant      none
// ==/UserScript==

// This must be loaded by Greasemonkey / Tampermonkey *before* any
// other page id helper userscripts, so that the helpers for specific
// sites override the definitions here.

page_id = function () {
    alert("No page_id() helper matched for " + location.href);
    return(null);
};

page_title = function () {
    return(document.title);
};

// This is required due to an apparent bug in gvfs:
// https://bugzilla.xfce.org/show_bug.cgi?id=10410
encodeURIComponent2 = function (s) {
    return(encodeURIComponent(s).replace(/'/g, "%27"));
};
