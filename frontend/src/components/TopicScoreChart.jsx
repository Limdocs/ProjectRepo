const STATUS_FILL_CLASS = {
  weak: 'course-page__weakness-chart-bar-fill--low',
  medium: 'course-page__weakness-chart-bar-fill--mid',
  strong: 'course-page__weakness-chart-bar-fill--high',
}

const STATUS_LABEL_KEYS = {
  weak: 'weaknessStatusWeak',
  medium: 'weaknessStatusMedium',
  strong: 'weaknessStatusStrong',
}

export default function TopicScoreChart({ topics, labels, txFn }) {
  if (!Array.isArray(topics) || topics.length === 0) return null

  return (
    <div className="course-page__weakness-chart-panel">
      <p className="course-page__weakness-chart-explainer">{labels.weaknessScoreExplainer}</p>
      <div
        className="course-page__weakness-chart-scroll"
        role="region"
        aria-label={labels.weaknessChartAriaLabel}
      >
        <div className="course-page__weakness-chart" role="list">
          {topics.map((item) => {
            const fillClass = STATUS_FILL_CLASS[item.status] ?? STATUS_FILL_CLASS.weak
            const statusLabelKey = STATUS_LABEL_KEYS[item.status]
            const statusLabel = statusLabelKey ? labels[statusLabelKey] : item.status
            const ariaLabel = txFn(labels.weaknessChartBarAria, {
              topic: item.displayLabel,
              score: item.score,
              status: statusLabel,
            })
            return (
              <div key={item.englishKey} className="course-page__weakness-chart-item" role="listitem">
                <div className="course-page__weakness-chart-bar-wrap">
                  <div
                    className="course-page__weakness-chart-bar-track"
                    role="img"
                    aria-label={ariaLabel}
                  >
                    <span
                      className={`course-page__weakness-chart-bar-fill ${fillClass}`}
                      style={{ height: `${item.score}%` }}
                    />
                  </div>
                  <span className="course-page__weakness-chart-score">{item.score}</span>
                </div>
                <span className="course-page__weakness-chart-label" title={item.displayLabel}>
                  {item.displayLabel}
                </span>
              </div>
            )
          })}
        </div>
      </div>
      <ul className="course-page__weakness-chart-legend" aria-label={labels.weaknessChartLegendAria}>
        <li className="course-page__weakness-chart-legend-item">
          <span
            className="course-page__weakness-chart-legend-swatch course-page__weakness-chart-bar-fill--low"
            aria-hidden
          />
          {labels.weaknessStatusWeak}
        </li>
        <li className="course-page__weakness-chart-legend-item">
          <span
            className="course-page__weakness-chart-legend-swatch course-page__weakness-chart-bar-fill--mid"
            aria-hidden
          />
          {labels.weaknessStatusMedium}
        </li>
        <li className="course-page__weakness-chart-legend-item">
          <span
            className="course-page__weakness-chart-legend-swatch course-page__weakness-chart-bar-fill--high"
            aria-hidden
          />
          {labels.weaknessStatusStrong}
        </li>
      </ul>
    </div>
  )
}
