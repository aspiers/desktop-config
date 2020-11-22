// ==UserScript==
// @name       Jenkins page id helpers
// @namespace  https://github.com/aspiers/desktop-config/tree/master/lib/browser/userscripts/page-id-helpers
// @version    0.1.6
// @description Functions to return identifiers for Jenkins build pages
// @match      https://ci.suse.de/*
// @match      https://ci.opensuse.org/*
// @author     Adam Spiers
// @downloadURL https://github.com/aspiers/desktop-config/raw/master/lib/browser/userscripts/page-id-helpers/Jenkins%20page%20id%20helpers.user.js
// @grant      none
// ==/UserScript==

window.jenkins_extract_metadata = function () {
    var m = document.title.match(/^(\S+) #(\d+): (.+?)( (Changes|Console))? \[Jenkins\]$/);
    if (m) {
        return({
            name: m[1],
            number: m[2],
            title: m[3],
        });
    }
    return null;
};

window.page_id = function () {
    var m = window.jenkins_extract_metadata();
    if (m) {
        return(m.name + "#" + m.number);
    }
    return(null);
};

window.page_title = function () {
    var m = window.jenkins_extract_metadata();
    if (m) {
        return(m.name + "#" + m.number + " (" + m.title + ")");
    }
    return(document.title);
};
