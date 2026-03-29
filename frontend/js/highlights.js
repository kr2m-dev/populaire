// highlights.js
// A module for extracting highlights from a given text

/**
 * Extract highlights from given text using specified criteria.
 * @param {string} text - The text to extract highlights from.
 * @param {RegExp} criteria - The criteria for highlights as a regex.
 * @returns {Array} - An array of highlighted segments.
 */
function extractHighlights(text, criteria) {
    const matches = text.match(criteria);
    return matches ? matches : [];
}

export { extractHighlights };