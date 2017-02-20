// ==UserScript==
// @name       Block Opensearch XML specs
// @namespace  *
// @version    0.3
// @description  Block opensearch xml links
// @match      *
// @copyright  2012+, Christian Huang
// ==/UserScript==

// From http://superuser.com/questions/276069/google-chrome-automatically-adding-websites-to-my-list-of-search-engines

var i;
var val;
var len;
var opensearches;

opensearches = document.getElementsByTagName('link');
len = opensearches.length;
for (i = 0; i < len;i++) {
    val = opensearches[i].type;
    if ( val == "application/opensearchdescription+xml") {
        opensearches[i].parentNode.removeChild(opensearches[i]);
    }
}
