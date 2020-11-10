// ==UserScript==
// @name       StoryBoard page id helpers
// @namespace  https://github.com/aspiers/desktop-config/tree/master/lib/browser/userscripts/page-id-helpers
// @version    0.0.3
// @description Functions to return identifiers for StoryBoard pages
// @match      https://storyboard.openstack.org/*
// @author     Adam Spiers
// @grant      none
// @downloadURL https://github.com/aspiers/desktop-config/raw/master/lib/browser/userscripts/page-id-helpers/StoryBoard%20page%20id%20helpers.user.js
// ==/UserScript==

window.StoryBoard_extract_metadata = function () {
    var m, meta = {};
    if (m = document.title.match(/^(.+) \| StoryBoard$/)) {
        meta.title = m[1];
    }
    if (m = location.href.match(/\/#!\/(board|project(?:_group)?|story|worklist)\/(\d+)/)) {
        meta.type = m[1];
        meta.num = m[2];
        meta.prefix = meta.type[0];
        if (meta.type == "project_group") {
            meta.prefix += "g";
        }
        meta.id = "sb" + meta.prefix + "#" + m[2];
    }
    if (meta.title || meta.id) {
        return meta;
    }
    return null;
};

window.page_id = function () {
    var m = window.StoryBoard_extract_metadata();
    if (m && m.id) {
        return(m.id);
    }
    return(null);
};

window.page_title = function () {
    var m = window.StoryBoard_extract_metadata();
    if (m) {
        if (m.id) {
            var title = m.id;
            if (m.title) {
                return m.id + " (" + m.title + ")";
            } else {
                return m.id;
            }
        } else {
            return m.title;
        }
    }
    return(document.title);
};

window.page_type = function () {
    var m = window.StoryBoard_extract_metadata();
    if (m && m.type) {
        return(m.type);
    }
    return(null);
};
