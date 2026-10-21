"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { usePlaidLink, type PlaidLinkOnExit, type PlaidLinkOnSuccess } from "react-plaid-link";
import { createLinkToken, errorMessage, exchangePublicToken, type ExchangeResult } from "@/lib/api";

type Props = {
  label: string;
  onLinked: (result: ExchangeResult) => void;
  onError: (message: string) => void;
};

// the token handoff: backend link_token -> Plaid Link (bank login happens at plaid) -> public_token -> backend exchange
export default function PlaidLinkButton({ label, onLinked, onError }: Props) {
  const [token, setToken] = useState<string | null>(null);
  const [tokenError, setTokenError] = useState<string | null>(null);
  const [exchanging, setExchanging] = useState(false);
  // one exchange per link, even if plaid calls back twice
  const inFlight = useRef(false);

  useEffect(() => {
    let cancelled = false;
    createLinkToken()
      .then((t) => !cancelled && setToken(t))
      .catch((err) => !cancelled && setTokenError(errorMessage(err)));
    return () => {
      cancelled = true;
    };
  }, []);

  const onSuccess = useCallback<PlaidLinkOnSuccess>(
    async (publicToken, metadata) => {
      if (inFlight.current) return;
      if (!publicToken) {
        onError("Plaid didn't return a token. Try linking again.");
        return;
      }
      inFlight.current = true;
      setExchanging(true);
      try {
        onLinked(await exchangePublicToken(publicToken, metadata.institution?.name));
      } catch (err) {
        onError(errorMessage(err));
      } finally {
        inFlight.current = false;
        setExchanging(false);
      }
    },
    [onLinked, onError],
  );

  // a null error just means the user closed Link
  const onExit = useCallback<PlaidLinkOnExit>(
    (err) => {
      if (err) onError(err.display_message ?? err.error_message);
    },
    [onError],
  );

  const { open, ready, error: scriptError } = usePlaidLink({ token, onSuccess, onExit });

  if (tokenError) {
    return <p className="text-sm text-red-600 dark:text-red-400">Couldn&apos;t start Link: {tokenError}</p>;
  }
  if (scriptError) {
    return <p className="text-sm text-red-600 dark:text-red-400">Couldn&apos;t load Plaid Link. Check your connection.</p>;
  }

  return (
    <button
      type="button"
      onClick={() => open()}
      disabled={!ready || exchanging}
      className="w-full rounded-md bg-neutral-900 px-4 py-2.5 text-sm font-medium text-white disabled:opacity-50 sm:w-auto dark:bg-white dark:text-neutral-900"
    >
      {exchanging ? "Linking…" : token ? label : "Loading…"}
    </button>
  );
}
