import {
  useEffect,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent as ReactKeyboardEvent,
} from "react";
import { motion } from "framer-motion";

// --- Shared glass/spring tokens (from the retired Pro tab, to keep visual voice) ---

const spring = { type: "spring" as const, stiffness: 400, damping: 30 };

const glassCard: CSSProperties = {
  background: "var(--surface-card)",
  border: "1px solid var(--border-hairline)",
  borderTop: "1px solid var(--border-emphasis)",
  boxShadow: "var(--shadow-glass-sm)",
};

// --- Icons ---

function MailIcon({ className }: { className?: string }) {
  return (
    <svg
      className={className}
      width="14"
      height="14"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <rect x="2" y="3.5" width="12" height="9" rx="1.5" />
      <path d="M2.5 5l5.5 3.75L13.5 5" />
    </svg>
  );
}

function BackArrowIcon({ className }: { className?: string }) {
  return (
    <svg
      className={className}
      width="14"
      height="14"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M10 3L5 8l5 5" />
    </svg>
  );
}

// --- Types ---

export interface SupportFormData {
  name: string;
  email: string;
  message: string;
}

interface SupportFormProps {
  initialName: string;
  initialEmail: string;
  initialMessage: string;
  onContinue: (data: SupportFormData) => void;
  onBack: () => void;
}

const EMAIL_REGEX = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

// --- Rounded-full input ---

interface PillInputProps {
  value: string;
  onChange: (v: string) => void;
  onKeyDown?: (e: ReactKeyboardEvent<HTMLInputElement>) => void;
  placeholder: string;
  ariaLabel: string;
  autoComplete?: string;
  type?: "text" | "email";
  ariaInvalid?: boolean;
  ariaDescribedBy?: string;
  inputRef?: React.RefObject<HTMLInputElement | null>;
}

function PillInput({
  value,
  onChange,
  onKeyDown,
  placeholder,
  ariaLabel,
  autoComplete,
  type = "text",
  ariaInvalid,
  ariaDescribedBy,
  inputRef,
}: PillInputProps) {
  return (
    <input
      ref={inputRef}
      type={type}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      onKeyDown={onKeyDown}
      placeholder={placeholder}
      aria-label={ariaLabel}
      autoComplete={autoComplete}
      aria-invalid={ariaInvalid || undefined}
      aria-describedby={ariaDescribedBy}
      className="w-full h-11 rounded-full px-6 text-[13px] font-body text-slate placeholder:text-steel/40 outline-none transition-all duration-300"
      style={{
        background: "var(--surface-control)",
        border: "1px solid var(--border-emphasis)",
        boxShadow: "inset 0 2px 4px rgba(0, 0, 0, 0.02)",
      }}
      onFocus={(e) => {
        // Option 2: focus = global outline only; no fill change on click.
        e.currentTarget.style.outline = "";
      }}
      onBlur={(e) => {
        e.currentTarget.style.background = "var(--surface-control)";
        e.currentTarget.style.boxShadow = "inset 0 2px 4px rgba(0, 0, 0, 0.02)";
        e.currentTarget.style.borderColor = "var(--border-emphasis)";
      }}
    />
  );
}

// --- SupportForm ---

export function SupportForm({
  initialName,
  initialEmail,
  initialMessage,
  onContinue,
  onBack,
}: SupportFormProps) {
  const [name, setName] = useState(initialName);
  const [email, setEmail] = useState(initialEmail);
  const [message, setMessage] = useState(initialMessage);
  const [textFocused, setTextFocused] = useState(false);

  const nameRef = useRef<HTMLInputElement | null>(null);
  const emailRef = useRef<HTMLInputElement | null>(null);
  const messageRef = useRef<HTMLTextAreaElement | null>(null);

  const emailValid = EMAIL_REGEX.test(email);
  const emailHasError = email.length > 0 && !emailValid;
  const formValid = Boolean(
    name.trim() && emailValid && message.trim().length >= 10,
  );

  // On mount, focus the first empty field.
  useEffect(() => {
    if (!name) {
      nameRef.current?.focus();
    } else if (!email) {
      emailRef.current?.focus();
    } else if (!message) {
      messageRef.current?.focus();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleContinue = () => {
    if (!formValid) return;
    onContinue({ name, email, message });
  };

  // Ctrl/Cmd+Enter anywhere in the form submits if valid.
  const handleFormKeyDown = (e: ReactKeyboardEvent<HTMLDivElement>) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter" && formValid) {
      e.preventDefault();
      handleContinue();
    }
  };

  // Enter in single-line name/email moves focus forward.
  const handleNameKeyDown = (e: ReactKeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter" && !e.ctrlKey && !e.metaKey) {
      e.preventDefault();
      emailRef.current?.focus();
    }
  };
  const handleEmailKeyDown = (e: ReactKeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter" && !e.ctrlKey && !e.metaKey) {
      e.preventDefault();
      messageRef.current?.focus();
    }
  };

  const emailHelperId = "support-email-helper";

  return (
    <motion.div
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ ...spring, delay: 0.03 }}
      className="rounded-3xl p-7"
      style={glassCard}
    >
      {/* Back link */}
      <button
        type="button"
        onClick={onBack}
        className="flex items-center gap-1.5 text-[12px] text-steel hover:text-slate transition-colors cursor-pointer mb-5"
      >
        <BackArrowIcon className="text-steel/70" />
        About
      </button>

      {/* Card header */}
      <div className="flex items-center gap-2 mb-1">
        <MailIcon className="text-steel" />
        <span className="text-[13px] font-medium text-slate">
          Contact support
        </span>
      </div>
      <p className="text-[12px] text-steel mb-5">
        Send a message. A human reads it.
      </p>

      <div className="flex flex-col gap-3" onKeyDown={handleFormKeyDown}>
        {/* Name */}
        <PillInput
          value={name}
          onChange={setName}
          onKeyDown={handleNameKeyDown}
          placeholder="Your name"
          ariaLabel="Your name"
          autoComplete="name"
          inputRef={nameRef}
        />

        {/* Email */}
        <div className="flex flex-col gap-1">
          <PillInput
            type="email"
            value={email}
            onChange={setEmail}
            onKeyDown={handleEmailKeyDown}
            placeholder="you@example.com"
            ariaLabel="Your email address"
            autoComplete="email"
            ariaInvalid={emailHasError}
            ariaDescribedBy={emailHasError ? emailHelperId : undefined}
            inputRef={emailRef}
          />
          {emailHasError && (
            <p
              id={emailHelperId}
              className="text-[11px] text-rose pl-6"
            >
              Check your email format.
            </p>
          )}
        </div>

        {/* Message */}
        <div className="relative">
          <div
            className="absolute bottom-0 left-[10%] right-[10%] h-[2px] rounded-full transition-all duration-500 ease-out pointer-events-none"
            style={{
              background: textFocused
                ? "linear-gradient(90deg, var(--color-frost), var(--color-frost-light), var(--color-frost))"
                : "transparent",
              opacity: textFocused ? 1 : 0,
            }}
          />
          <textarea
            ref={messageRef}
            value={message}
            onChange={(e) => setMessage(e.target.value)}
            onFocus={() => setTextFocused(true)}
            onBlur={() => setTextFocused(false)}
            rows={5}
            placeholder="What's going on? Detail helps us reproduce it."
            aria-label="Support message"
            className="w-full px-4 py-3 rounded-2xl text-[13px] text-slate outline-none placeholder:text-steel/30 transition-all duration-300 resize-none"
            style={{
              background: textFocused
                ? "var(--surface-hover)"
                : "var(--surface-control)",
              border: "none",
              caretColor: "var(--color-frost-deep)",
            }}
          />
        </div>

        {/* Continue button */}
        <div className="flex mt-2">
          <button
            type="button"
            onClick={handleContinue}
            disabled={!formValid}
            aria-disabled={!formValid || undefined}
            className="h-11 px-7 rounded-full text-[13px] font-medium text-white shrink-0 cursor-pointer transition-all duration-200 disabled:opacity-50 disabled:cursor-not-allowed"
            style={{
              background: "var(--accent-fill)",
              boxShadow:
                "0 0 12px rgba(255, 255, 255, 0.6), var(--shadow-frost-glow)",
            }}
            onMouseEnter={(e) => {
              if (formValid) {
                e.currentTarget.style.transform = "translateY(-1px)";
                e.currentTarget.style.boxShadow =
                  "var(--shadow-frost-glow), inset 0 1px 0 rgb(255 255 255 / calc(0.6 * var(--rim-scale)))";
              }
            }}
            onMouseLeave={(e) => {
              e.currentTarget.style.transform = "translateY(0)";
              e.currentTarget.style.boxShadow =
                "0 0 12px rgba(255, 255, 255, 0.6), var(--shadow-frost-glow)";
            }}
          >
            Next
          </button>
        </div>
      </div>
    </motion.div>
  );
}

export default SupportForm;
