export default {
  async fetch(request, env) {
    if (request.method !== "POST") {
      return new Response("Method Not Allowed", {
        status: 405,
      });
    }

    const expectedToken = env.TOILIX_PROXY_TOKEN;
    const suppliedToken = request.headers.get("X-Proxy-Token");

    if (!expectedToken || suppliedToken !== expectedToken) {
      return new Response("Unauthorized: proxy token mismatch", {
        status: 401,
      });
    }

    const url = new URL(request.url);
    const model = url.searchParams.get("model");

    if (!model || !/^[a-zA-Z0-9._-]+$/.test(model)) {
      return new Response("Invalid model", {
        status: 400,
      });
    }

    const apiKey = request.headers.get("X-Gemini-Key");

    if (!apiKey) {
      return new Response("Missing Gemini API key", {
        status: 400,
      });
    }

    let body;

    try {
      body = await request.text();
      JSON.parse(body);
    } catch {
      return new Response("Invalid JSON body", {
        status: 400,
      });
    }

    try {
      const response = await fetch(
        `https://generativelanguage.googleapis.com/v1beta/models/${encodeURIComponent(model)}:generateContent`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "x-goog-api-key": apiKey,
          },
          body,
        }
      );

      return new Response(await response.text(), {
        status: response.status,
        headers: {
          "Content-Type":
            response.headers.get("Content-Type") || "application/json",
          "Cache-Control": "no-store",
        },
      });
    } catch {
      return new Response("Upstream request failed", {
        status: 502,
      });
    }
  },
};
