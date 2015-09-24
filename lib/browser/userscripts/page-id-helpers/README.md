# Userscripts and bookmarklets for generating references/links to web pages

Use Linux?  Fed up of manually copying and pasting references and
hyperlinks to web pages?  We can help!  Well - with the copying part,
at least.  You still have to paste the result where you need it,
since noone's figured out how to write telepathic code yet.

This directory contains userscripts which provide Javascript helper
functions allowing the easy creation of bookmarklets which
automatically generate references/hyperlinks to web pages, and then in
conjunction with a custom protocol handler, copy them to your Xorg
clipboard and/or into your running `emacs`.

For example, if I visit https://github.com/aspiers/git-deps/issues/37,
via a single click of a bookmarklet this Markdown-formatted link will
be copied to my clipboard ready for pasting:

    [aspiers/git-deps#37: detect whether commit A depends on commit B](https://github.com/aspiers/git-deps/issues/37)

Notice how the link text is formatted, with the github organization,
repository name, and issue number all nicely combined in [the canonical
github reference format](https://help.github.com/articles/writing-on-github/#references).

There are currently backends for github, bugzilla, Jenkins, etherpad,
and [FATE](https://fate.suse.com).  The next one to add will probably
be Trello.

Similarly I have a
[custom search engine](http://www.slideshare.net/mauilibrarian2/create-a-custom-search-engine-in-chromes-omnibox)
in my Chrome settings which uses the same bookmarklet link, so I can
achieve the same thing from the keyboard only by typing `Control-L` to
focus the address bar, and then `ml <Enter>` to activate the search
engine (`ml` is the keyboard shortcut I chose for this search engine,
as a mnemonic for "Markdown Link").

## Compatibility

The userscripts are tested in Chrome with
[Tampermonkey](https://tampermonkey.net/), but in theory they should
work fine with
[Greasemonkey](https://addons.mozilla.org/en-us/firefox/addon/greasemonkey/)
in Firefox too.

The protocol handlers are built for use on Linux, but you could implement
an alternative protocol handler to achieve the same effect on another OS.

Here are the bookmarklets, grouped into two types.

## 1. Bookmarklets which use a custom protocol handler for `xclip://...` URLs

-   Copy `document.title`
    -    `javascript:location.href='xclip://'+encodeURIComponent2(document.title)`
-   Copy a Markdown-formatted link to the page
    -    `javascript:location.href='xclip://'+encodeURIComponent2('[' + page_title() + '](' + location.href + ')')`
-   Copy a short identifier for the page
    -    `javascript:location.href = "xclip://" + encodeURIComponent2(page_id())`
-   Copy a longer identifier for the page (i.e. a formatted version of `document.title`)
    -    `javascript:location.href='xclip://'+encodeURIComponent2(page_title())`

### Installation

For these to work, you need to do the following:

-   Ensure you have `xclip` installed.
-   Install the userscripts in your browser.  Make sure the 
    [default helpers](00 default page id helpers.user.js) are before
    the others in the execution order.
-   Install [`xclip-handler`](../../../../bin/xclip-handler) as an
    executable script somewhere on your `$PATH` (e.g. `~/bin`).
-   Install [`xclip-handler.desktop`](../../../../.local/share/applications/xclip-handler.desktop)
    in `~/.local/share/applications/`.
-   Run [this setup code](../../../../.cfg-post.d/xclip-handler).
-   Create bookmarklets and/or
    [custom search engines](http://www.slideshare.net/mauilibrarian2/create-a-custom-search-engine-in-chromes-omnibox).

## 2. Bookmarklets which use a custom protocol handler for `org-protocol://...` URLs

These are only useful if you use `emacs` and [Org mode](http://orgmode.org/) ...
and if you don't, what the hell are you thinking? ;-)

-   org store link
    -   `javascript:location.href='org-protocol://store-link://'+encodeURIComponent2(location.href)+'/'+encodeURIComponent2(page_title())+'/'+encodeURIComponent2(window.getSelection())`
-   org capture
    -   `javascript:location.href='org-protocol://capture://'+encodeURIComponent2(location.href)+'/'+encodeURIComponent2(page_title())+'/'+encodeURIComponent2(window.getSelection())`

### Installation

http://orgmode.org/worg/org-contrib/org-protocol.html is a good
starting point for learning about `org-protocol`, but I haven't yet
fixed the installation instructions for Linux, which only work for
KDE4 and GNOME's now outdated gconf-based `url-handler` mechanism.

However you can see
[my handler](https://github.com/aspiers/emacs/blob/master/.local/share/applications/quick-emacs.desktop)
and
[the corresponding setup code](https://github.com/aspiers/emacs/blob/master/.cfg-post.d/org-protocol).
