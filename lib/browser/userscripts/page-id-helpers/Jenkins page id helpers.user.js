// ==UserScript==
// @name       Jenkins page id helpers
// @namespace  http://adamspiers.org/
// @version    0.1.1
// @description Functions to return identifiers for Jenkins build pages
// @match      https://ci.suse.de/*
// @match      https://ci.opensuse.org/*
// @author     2015 Adam Spiers
// @downloadURL https://github.com/aspiers/desktop-config/raw/master/lib/browser/userscripts/page-id-helpers/Jenkins%20page%20id%20helpers.user.js
// @grant      none
// ==/UserScript==

jenkins_extract_metadata = function () {
    var m = document.title.match(/^(\S+) #(\d+): (.+?)( (Changes|Console))? \[Jenkins\]$/);
    if (m) {
        return({
            name: m[1],
            number: m[1],
            title: m[2],
        });
    }
    return null;
};

page_id = function () {
    var m = jenkins_extract_metadata();
    if (m) {
        return(m.name + "#" + m.number);
    }
    return(null);
};

page_title = function () {
    var m = jenkins_extract_metadata();
    if (m) {
        return(m.name + "#" + m.number + ": " + m.title);
    }
    return(document.title);
};
