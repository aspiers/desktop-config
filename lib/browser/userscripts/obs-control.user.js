// ==UserScript==
// @name         OBS Studio Control
// @namespace    http://tampermonkey.net/
// @version      1.0
// @description  Control OBS Studio via web UI (updated for new webui design)
// @author       Adam Spiers
// @match        http*://*/obs/*
// @match        http*://127.0.0.1:4445/*
// @match        http*://localhost:4445/*
// @grant        none
// ==/UserScript==

(function() {
    'use strict';

    // Updated selectors for new OBS webui design
    const selectors = {
        // New webui uses data attributes instead of class-based selectors
        streamButton: '[data-testid="stream-btn"], #stream-button, .stream-control',
        sceneSelector: '[data-testid="scene-list"], #scene-selector, .scene-list',
        audioMixer: '[data-testid="audio-mixer"], #audio-mixer, .audio-mixer',
        sourcesList: '[data-testid="sources-list"], #sources, .sources-list'
    };

    // Wait for DOM to load
    window.addEventListener('DOMContentLoaded', () => {
        // Stream control
        const streamBtn = document.querySelector(selectors.streamButton);
        if (streamBtn) {
            streamBtn.addEventListener('click', () => {
                console.log('[OBS Control] Stream toggled');
            });
        }

        // Scene switching
        const sceneSelector = document.querySelector(selectors.sceneSelector);
        if (sceneSelector) {
            sceneSelector.addEventListener('change', (e) => {
                console.log('[OBS Control] Scene changed to:', e.target.value);
            });
        }
    });
})();
