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
      results: [],
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
      }
    });
    socket.on("connect_error", () => {
      this.connected = false;
      this.error = "Cannot connect to the game. Retrying…";
    });
    socket.on("judgment:history", (payload) => {
      if (payload && Array.isArray(payload.results)) {
        this.addResults(payload.results);
        if (this.pending && this.results.some((result) => result.request_id === this.pending)) {
          this.pending = null;
          this.subject = "";
        }
      }
    });
    socket.on("judgment:result", (result) => {
      this.addResults([result]);
      if (result.request_id === this.pending) {
        this.pending = null;
        this.subject = "";
        this.error = "";
      }
    });
    socket.on("judgment:error", (payload) => {
      if (payload && payload.request_id === this.pending) {
        this.pending = null;
        this.error = payload.message || "The judgment failed. Please try again.";
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
      if (!this.normalizedHistoryQuery) return this.results;
      return this.results.filter((result) => {
        const subject = result && typeof result.subject === "string" ? result.subject : "";
        const verdict = result && typeof result.choice === "string" ? this.label(result.choice) : "";
        return `${subject} ${verdict}`.toLowerCase().includes(this.normalizedHistoryQuery);
      });
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
    clearHistoryQuery() {
      this.historyQuery = "";
    },
  },
}).mount("#app");
