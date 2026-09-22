/* global Vue, io */
"use strict";

const socket = io();

Vue.createApp({
  delimiters: ["[[", "]]"],
  data() {
    return {
      subject: "",
      connected: false,
      pending: null,
      error: "",
      results: [],
      options: [
        { key: "heaven", label: "Heaven" },
        { key: "hell", label: "Hell" },
        { key: "purgatory", label: "Purgatory" },
      ],
    };
  },
  mounted() {
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
  methods: {
    submit() {
      if (!this.connected || this.pending || !this.subject.trim()) return;
      this.error = "";
      this.pending = crypto.randomUUID();
      socket.emit("judgment:submit", { request_id: this.pending, subject: this.subject });
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
  },
}).mount("#app");
