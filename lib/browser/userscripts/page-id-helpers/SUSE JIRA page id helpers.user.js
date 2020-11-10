// ==UserScript==
// @name       SUSE JIRA page id helpers
// @namespace  https://github.com/aspiers/desktop-config/tree/master/lib/browser/userscripts/page-id-helpers
// @version    0.0.5
// @description Functions to return identifiers for SUSE JIRA pages
// @match      https://suse-jira.dyndns.org/*
// @author     Adam Spiers
// @grant      none
// @downloadURL https://github.com/aspiers/desktop-config/raw/master/lib/browser/userscripts/page-id-helpers/SUSE%20JIRA%20page%20id%20helpers.user.js
// ==/UserScript==

window.SUSE_JIRA_extract_metadata = function () {
    var m = document.title.match(/^\[([^\]]+)\] (.+) - SUSE Jira$/);
    if (m) {
        return({
            id: m[1],
            title: m[2],
        });
    }
    return null;
};

window.page_id = function () {
    var m = window.SUSE_JIRA_extract_metadata();
    if (m) {
        return(m.id);
    }
    return(null);
};

window.page_title = function () {
    var m = window.SUSE_JIRA_extract_metadata();
    if (m) {
        return(m.id + " (" + m.title + ")");
    }
    return(document.title);
};
