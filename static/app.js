/* global Vue, io */
"use strict";

const socket = io();
const PURGATORY_STORAGE_KEY = "hotornot.enablePurgatory";

Vue.createApp({
  delimiters: ["[[", "]]"],
  data() {
    return {
      subject: "",
      enablePurgatory: true,
      connected: false,
      pending: null,
      error: "",
      historyQuery: "",
      historySort: "newest",
      results: [],
      historySortOptions: [
        { key: "newest", label: "Newest" },
        { key: "last_name", label: "Last name" },
        { key: "confidence", label: "Confidence" },
        { key: "outcome", label: "Outcome" },
      ],
      options: [
        { key: "heaven", label: "Heaven" },
        { key: "hell", label: "Hell" },
        { key: "purgatory", label: "Purgatory" },
      ],
    };
  },
  mounted() {
    this.enablePurgatory = localStorage.getItem(PURGATORY_STORAGE_KEY) !== "false";
    socket.on("connect", () => {
      this.connected = true;
      this.error = "";
    });
    socket.on("disconnect", () => {
      this.connected = false;
      if (this.pending) {
        this.pending = null;
        this.error = "Connection lost. Reconnect and check the history before trying again.";
        this.focusSubjectInput();
      }
    });
    socket.on("connect_error", () => {
      this.connected = false;
      this.error = "Cannot connect to the game. Retrying…";
    });
    socket.on("judgment:history", (payload) => {
      if (payload && Array.isArray(payload.results)) {
        this.results = this.sortedResults(payload.results);
        if (this.pending && this.results.some((result) => result.request_id === this.pending)) {
          this.pending = null;
          this.subject = "";
          this.focusSubjectInput();
        }
      }
    });
    socket.on("judgment:result", (result) => {
      this.addResults([result]);
      if (result.request_id === this.pending) {
        this.pending = null;
        this.subject = "";
        this.error = "";
        this.focusSubjectInput();
      }
    });
    socket.on("judgment:error", (payload) => {
      if (payload && payload.request_id === this.pending) {
        this.pending = null;
        this.error = payload.message || "The judgment failed. Please try again.";
        this.focusSubjectInput();
      }
    });
  },
  watch: {
    enablePurgatory(value) {
      localStorage.setItem(PURGATORY_STORAGE_KEY, value ? "true" : "false");
    },
  },
  computed: {
    normalizedHistoryQuery() {
      return this.historyQuery.trim().toLowerCase();
    },
    filteredResults() {
      const results = this.normalizedHistoryQuery
        ? this.results.filter((result) => {
            const subject = result && typeof result.subject === "string" ? result.subject : "";
            const verdict = result && typeof result.choice === "string" ? this.label(result.choice) : "";
            return `${subject} ${verdict}`.toLowerCase().includes(this.normalizedHistoryQuery);
          })
        : this.results;
      return this.sortedResults(results);
    },
  },
  methods: {
    submit() {
      if (!this.connected || this.pending || !this.subject.trim()) return;
      this.error = "";
      this.pending = crypto.randomUUID();
      socket.emit("judgment:submit", {
        request_id: this.pending,
        subject: this.subject,
        enable_purgatory: this.enablePurgatory,
      });
    },
    addResults(incoming) {
      const byId = new Map(this.results.map((result) => [result.id, result]));
      for (const result of incoming) {
        if (result && result.id && !byId.has(result.id)) byId.set(result.id, result);
      }
      this.results = Array.from(byId.values()).sort((a, b) => b.sequence - a.sequence);
    },
    percentage(value) {
      return Math.round(value * 1000) / 10;
    },
    label(key) {
      return this.options.find((option) => option.key === key)?.label || key;
    },
    resultOptions(result) {
      if (!result || !result.probabilities) return [];
      return this.options.filter((option) => Object.hasOwn(result.probabilities, option.key));
    },
    sortedResults(results) {
      const sorted = [...results];
      const newestFirst = (a, b) => this.sequenceFor(b) - this.sequenceFor(a);
      if (this.historySort === "last_name") {
        return sorted.sort(
          (a, b) =>
            this.lastNameFor(a).localeCompare(this.lastNameFor(b), undefined, { sensitivity: "base" }) ||
            this.subjectFor(a).localeCompare(this.subjectFor(b), undefined, { sensitivity: "base" }) ||
            newestFirst(a, b),
        );
      }
      if (this.historySort === "confidence") {
        return sorted.sort(
          (a, b) => this.numberFor(b.confidence) - this.numberFor(a.confidence) || newestFirst(a, b),
        );
      }
      if (this.historySort === "outcome") {
        return sorted.sort(
          (a, b) =>
            this.label(a.choice).localeCompare(this.label(b.choice), undefined, { sensitivity: "base" }) ||
            newestFirst(a, b),
        );
      }
      return sorted.sort(newestFirst);
    },
    subjectFor(result) {
      return result && typeof result.subject === "string" ? result.subject.trim() : "";
    },
    lastNameFor(result) {
      const words = this.subjectFor(result).split(/\s+/).filter(Boolean);
      return words.at(-1) || "";
    },
    numberFor(value) {
      return typeof value === "number" && Number.isFinite(value) ? value : -Infinity;
    },
    sequenceFor(result) {
      return this.numberFor(result?.sequence);
    },
    focusSubjectInput() {
      this.$nextTick(() => {
        this.$refs.subjectInput?.focus();
      });
    },
    clearHistoryQuery() {
      this.historyQuery = "";
    },
  },
}).mount("#app");
