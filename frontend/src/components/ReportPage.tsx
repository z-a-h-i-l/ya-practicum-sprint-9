import React, { useState, useEffect, useCallback } from 'react';

interface TelemetryReport {
  report: Record<string, any>;
  customer_id: string;
  message: string;
}

interface ReportPeriod {
  period_start: string;
  period_end: string;
  generated_at: string;
}

const API_URL = process.env.REACT_APP_API_URL || 'http://localhost:8000';
const AUTH_URL = process.env.REACT_APP_AUTH_URL || 'http://localhost:8081';

function ReportPage() {
  const [report, setReport] = useState<TelemetryReport | null>(null);
  const [periods, setPeriods] = useState<ReportPeriod[]>([]);
  const [error, setError] = useState<string>('');
  const [isAuthenticated, setIsAuthenticated] = useState(false);
  const [username, setUsername] = useState('');
  const [loading, setLoading] = useState(true);
  const [fetchingReport, setFetchingReport] = useState(false);
  const [selectedPeriodStart, setSelectedPeriodStart] = useState('');
  const [selectedPeriodEnd, setSelectedPeriodEnd] = useState('');

  const checkAuth = useCallback(async () => {
    try {
      const response = await fetch(`${AUTH_URL}/api/auth/session`, {
        method: 'GET',
        credentials: 'include',
      });
      if (response.ok) {
        const data = await response.json();
        setIsAuthenticated(true);
        setUsername(data.username || '');
        return true;
      } else {
        setIsAuthenticated(false);
        setUsername('');
        return false;
      }
    } catch {
      setIsAuthenticated(false);
      setUsername('');
      return false;
    }
  }, []);

  useEffect(() => {
    checkAuth().finally(() => setLoading(false));
  }, [checkAuth]);

  const fetchReport = useCallback(async (periodStart?: string, periodEnd?: string) => {
    setFetchingReport(true);
    setError('');
    try {
      let url = `${API_URL}/reports`;
      const params = new URLSearchParams();
      if (periodStart) params.append('period_start', periodStart);
      if (periodEnd) params.append('period_end', periodEnd);
      if (params.toString()) url += '?' + params.toString();

      const response = await fetch(url, {
        method: 'GET',
        credentials: 'include',
      });
      if (response.ok) {
        const data: TelemetryReport = await response.json();
        setReport(data);
        setError('');
      } else if (response.status === 401) {
        setIsAuthenticated(false);
        setError('Session expired. Please log in again.');
      } else {
        const errData = await response.json();
        setError(errData.detail || 'Failed to fetch report');
      }
    } catch (err) {
      setError('Failed to connect to the reports server');
    } finally {
      setFetchingReport(false);
    }
  }, []);

  const fetchAvailablePeriods = useCallback(async () => {
    try {
      const response = await fetch(`${API_URL}/reports/periods`, {
        method: 'GET',
        credentials: 'include',
      });
      if (response.ok) {
        const data = await response.json();
        setPeriods(data.available_periods || []);
      }
    } catch {
      // Not critical — ignore
    }
  }, []);

  const handleLogin = () => {
    window.location.href = `${AUTH_URL}/api/auth/login`;
  };

  const handleLogout = async () => {
    try {
      const response = await fetch(`${AUTH_URL}/api/auth/logout`, {
        method: 'POST',
        credentials: 'include',
      });
      const data = await response.json();
      if (data.logout_url) {
        window.location.href = data.logout_url;
        return;
      }
    } catch {
      // ignore
    }
    setIsAuthenticated(false);
    setUsername('');
    setReport(null);
    setPeriods([]);
  };

  if (loading) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <div className="text-gray-500">Loading...</div>
      </div>
    );
  }

  if (!isAuthenticated) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-gray-50">
        <div className="bg-white p-8 rounded-lg shadow-md w-96">
          <h1 className="text-2xl font-bold mb-6 text-center">BionicPRO</h1>
          <h2 className="text-xl mb-4 text-center text-gray-600">Authentication Required</h2>
          <p className="mb-6 text-gray-600 text-center">
            Please sign in via Keycloak to access your reports.
          </p>
          {error && (
            <div className="mb-4 text-red-500 text-sm text-center">{error}</div>
          )}
          <button
            onClick={handleLogin}
            className="w-full bg-blue-500 text-white py-2 px-4 rounded hover:bg-blue-600 focus:outline-none"
          >
            Sign In with Keycloak
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="container mx-auto px-4 py-8">
      <div className="flex justify-between items-center mb-8">
        <h1 className="text-3xl font-bold">BionicPRO Reports</h1>
        <div className="flex items-center gap-4">
          <span className="text-gray-600">Welcome, {username}</span>
          <button
            onClick={handleLogout}
            className="bg-red-500 text-white py-2 px-4 rounded hover:bg-red-600"
          >
            Logout
          </button>
        </div>
      </div>

      {error && (
        <div className="bg-red-100 border border-red-400 text-red-700 px-4 py-3 rounded mb-4">
          {error}
        </div>
      )}

      {/* Fetch latest report button */}
      <div className="mb-6 flex flex-wrap items-center gap-4">
        <button
          onClick={() => fetchReport()}
          disabled={fetchingReport}
          className="bg-green-500 text-white py-2 px-6 rounded hover:bg-green-600 disabled:opacity-50 disabled:cursor-not-allowed focus:outline-none"
        >
          {fetchingReport ? 'Loading...' : 'Generate My Report'}
        </button>

        <button
          onClick={fetchAvailablePeriods}
          className="bg-blue-500 text-white py-2 px-4 rounded hover:bg-blue-600 focus:outline-none"
        >
          Show Available Periods
        </button>
      </div>

      {/* Available periods */}
      {periods.length > 0 && (
        <div className="mb-6 bg-white p-4 rounded-lg shadow">
          <h3 className="text-lg font-semibold mb-3">Available Report Periods (processed by Airflow)</h3>
          <div className="space-y-2">
            {periods.map((p, idx) => (
              <div key={idx} className="flex items-center gap-4 text-sm">
                <span className="text-gray-600">
                  {new Date(p.period_start).toLocaleDateString()} — {new Date(p.period_end).toLocaleDateString()}
                </span>
                <span className="text-gray-400 text-xs">
                  generated: {new Date(p.generated_at).toLocaleString()}
                </span>
                <button
                  onClick={() => fetchReport(p.period_start, p.period_end)}
                  className="text-blue-500 hover:text-blue-700 text-xs underline"
                >
                  Load this report
                </button>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Custom period selector */}
      <div className="mb-6 bg-white p-4 rounded-lg shadow">
        <h3 className="text-lg font-semibold mb-3">Custom Period</h3>
        <div className="flex flex-wrap items-end gap-4">
          <div>
            <label className="block text-sm text-gray-600 mb-1">Start Date</label>
            <input
              type="date"
              value={selectedPeriodStart}
              onChange={(e) => setSelectedPeriodStart(e.target.value)}
              className="border rounded px-3 py-1 text-sm"
            />
          </div>
          <div>
            <label className="block text-sm text-gray-600 mb-1">End Date</label>
            <input
              type="date"
              value={selectedPeriodEnd}
              onChange={(e) => setSelectedPeriodEnd(e.target.value)}
              className="border rounded px-3 py-1 text-sm"
            />
          </div>
          <button
            onClick={() => fetchReport(selectedPeriodStart, selectedPeriodEnd)}
            disabled={fetchingReport || !selectedPeriodStart || !selectedPeriodEnd}
            className="bg-purple-500 text-white py-1 px-4 rounded hover:bg-purple-600 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            Fetch
          </button>
        </div>
      </div>

      {/* Report display */}
      {!report && !error && !fetchingReport && (
        <p className="text-gray-500 text-center mt-8">
          Click "Generate My Report" to load your telemetry report
        </p>
      )}

      {report && (
        <div className="bg-white p-6 rounded-lg shadow mb-6">
          <h2 className="text-xl font-semibold mb-4">
            Telemetry Report for Customer {report.customer_id}
          </h2>
          <div className="overflow-x-auto">
            <table className="min-w-full border-collapse">
              <thead>
                <tr className="bg-gray-100">
                  <th className="border px-4 py-2 text-left text-sm font-medium text-gray-700">Field</th>
                  <th className="border px-4 py-2 text-left text-sm font-medium text-gray-700">Value</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(report.report).map(([key, value]) => (
                  <tr key={key} className="even:bg-gray-50">
                    <td className="border px-4 py-2 text-sm font-medium text-gray-600">{key}</td>
                    <td className="border px-4 py-2 text-sm text-gray-800">
                      {value === null ? (
                        <span className="text-gray-400 italic">N/A</span>
                      ) : typeof value === 'string' && (key.endsWith('_ts') || key.endsWith('_at') || key.includes('date') || key.includes('period')) ? (
                        new Date(value as string).toLocaleString()
                      ) : (
                        String(value)
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Note about data freshness */}
      <div className="bg-yellow-50 border border-yellow-200 text-yellow-800 px-4 py-3 rounded text-sm">
        <strong>Note:</strong> Reports are generated by Airflow daily at 02:00 UTC.
        Data for the current day may not yet be available.
        Requesting data not yet processed will return a 404 error.
      </div>
    </div>
  );
}

export default ReportPage;
