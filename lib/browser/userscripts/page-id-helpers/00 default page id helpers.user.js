// ==UserScript==
// @name       default page id helpers
// @namespace  https://github.com/aspiers/desktop-config/tree/master/lib/browser/userscripts/page-id-helpers
// @version    0.2.0
// @description Functions to return default identifiers for all pages
// @match      *://*/*
// @author     Adam Spiers
// @grant      none
// @downloadURL https://github.com/aspiers/desktop-config/raw/master/lib/browser/userscripts/page-id-helpers/00%20default%20page%20id%20helpers.user.js
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

xclip = function (url) {
    url = "xclip://" + encodeURIComponent2(url);

    // This method seems somewhat unreliable:
    location.href = url;

    // This one is more reliable, but I can't get the new window to close,
    // even with @grant window.close:
    // var w = window.open();
    // w.onload = function () {
    //     w.close();
    // };
};
