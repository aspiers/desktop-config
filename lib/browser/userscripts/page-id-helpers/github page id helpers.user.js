// ==UserScript==
// @name       github page id helpers
// @namespace  https://github.com/aspiers/desktop-config/tree/master/lib/browser/userscripts/page-id-helpers
// @version    0.1.1
// @description Functions to return identifiers for github pages
// @match      https://github.com/*
// @author     2015 Adam Spiers
// @grant      none
// @downloadURL https://github.com/aspiers/desktop-config/raw/master/lib/browser/userscripts/page-id-helpers/github%20page%20id%20helpers.user.js
// ==/UserScript==

github_extract_metadata = function () {
    var m = document.title.match(/^(.+) by (.+) · Pull Request (#\d+) · ([^/]+)\/(.+)$/);
    if (m) {
        return({
            title: m[1],
            author: m[2],
            number: m[3],
            org: m[4],
            repo: m[5],
        });
    }

    m = document.title.match(/^(.+) · Issue (#\d+) · ([^/]+)\/(.+)$/);
    if (m) {
        return({
            title: m[1],
            number: m[2],
            org: m[3],
            repo: m[4],
        });
    }

    return null;
};

page_id = function () {
    var m = github_extract_metadata();
    if (m) {
        return(m.org + "/" + m.repo + m.number);
    }
    return(null);
};

page_title = function () {
    var m = github_extract_metadata();
    if (m) {
        return(m.org + "/" + m.repo + m.number + ": " + m.title);
    }
    return(document.title);
};
