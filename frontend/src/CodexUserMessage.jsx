import { useId, useLayoutEffect, useRef, useState } from "react";
import { ChevronDown } from "lucide-react";

export default function CodexUserMessage({ content }) {
  const contentId = useId();
  const body = useRef(null);
  const toggleButton = useRef(null);
  const [expanded, setExpanded] = useState(false);
  const [collapsible, setCollapsible] = useState(false);

  useLayoutEffect(() => {
    const element = body.current;
    const measure = () => {
      const style = getComputedStyle(element);
      const limit =
        Number(style.getPropertyValue("--codex-user-preview-lines")) *
        parseFloat(style.lineHeight);
      setCollapsible(element.getBoundingClientRect().height > limit + 1);
    };
    measure();
    // Measure the unclipped body so expansion and width changes use the same limit.
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, [content]);

  function toggle() {
    setExpanded(!expanded);
    if (expanded)
      requestAnimationFrame(() => {
        toggleButton.current?.focus({ preventScroll: true });
        toggleButton.current?.scrollIntoView({ block: "nearest" });
      });
  }

  return (
    <article className="codex-message user">
      <div className="codex-user-heading">
        <span className="codex-role">你</span>
        {collapsible && (
          <button
            ref={toggleButton}
            type="button"
            className="codex-user-toggle"
            aria-expanded={expanded}
            aria-controls={contentId}
            onClick={toggle}
          >
            {expanded ? "收起" : "展开全文"}
            <ChevronDown size={13} />
          </button>
        )}
      </div>
      <div
        id={contentId}
        className="codex-user-preview"
        data-expanded={expanded}
        data-folded={collapsible && !expanded}
      >
        <div ref={body} className="codex-user-content">
          {(content || []).map((part, index) => (
            <p key={index}>
              {part.text ||
                (part.type === "image"
                  ? "[图片]"
                  : part.type === "skill"
                    ? `$${part.name}`
                    : `[${part.type}]`)}
            </p>
          ))}
        </div>
      </div>
      {collapsible && expanded && (
        <button
          type="button"
          className="codex-user-toggle codex-user-collapse"
          aria-expanded={expanded}
          aria-controls={contentId}
          onClick={toggle}
        >
          收起长消息
          <ChevronDown size={13} />
        </button>
      )}
    </article>
  );
}
