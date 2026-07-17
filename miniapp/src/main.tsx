import { render } from "preact";

import { App } from "./app";
import "./styles.css";

const root = document.getElementById("app");
if (!root) {
  throw new Error("Mini App root element is missing.");
}

render(<App />, root);
