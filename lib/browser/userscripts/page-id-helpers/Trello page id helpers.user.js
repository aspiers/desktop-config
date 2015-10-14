// ==UserScript==
// @name       Trello page id helpers
// @namespace  http://adamspiers.org/
// @version    0.1
// @description Functions to return identifiers for Trello cards
// @match      https://trello.com/*
// @author     2015 Adam Spiers
// @grant      none
// ==/UserScript==

trello_extract_title = function () {
    var m = document.title.match(/^(?:\((\d+)\) )?(.+) on (.+) | Trello$/);
    if (m) {
        return(m[2] + " Trello");
    }
    return(document.title);
};

page_id = trello_extract_title;

page_title = trello_extract_title;
